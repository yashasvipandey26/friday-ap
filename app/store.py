"""Everything that is remembered: the procurement data the rules check against, and the run history.

SQLite keeps the whole thing in one file with no setup. All SQL lives here, so swapping in
Postgres later means rewriting this module and nothing else.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .domain import (
    LedgerEntry,
    PoBalance,
    PurchaseOrder,
    QueuedInvoice,
    StageProgress,
    Vendor,
    reference_key,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, tax_id TEXT NOT NULL, bank_name TEXT NOT NULL,
    bank_account TEXT NOT NULL, bank_ifsc TEXT NOT NULL, phone TEXT NOT NULL, email TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_number TEXT PRIMARY KEY, po_key TEXT NOT NULL, vendor_id TEXT NOT NULL, description TEXT NOT NULL,
    currency TEXT NOT NULL, amount REAL NOT NULL, tax_rate_percent REAL NOT NULL, status TEXT NOT NULL,
    order_date TEXT NOT NULL, lines TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id TEXT NOT NULL, invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL, pre_tax REAL NOT NULL, total REAL NOT NULL, po_number TEXT,
    posted_at TEXT NOT NULL, recorded_by TEXT NOT NULL, run_id INTEGER
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, finished_at TEXT,
    filename TEXT NOT NULL, file_path TEXT NOT NULL, media_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running', stages TEXT NOT NULL DEFAULT '[]',
    invoice TEXT, decision TEXT, explanation TEXT, outcome TEXT,
    vendor_id TEXT, vendor_name TEXT, invoice_number TEXT, invoice_key TEXT, invoice_date TEXT,
    currency TEXT, pre_tax REAL, total REAL, po_number TEXT, duration_ms INTEGER,
    resolution TEXT, resolution_note TEXT, resolved_at TEXT
);
"""

_RUN_JSON_COLUMNS = ("stages", "invoice", "decision", "explanation")
_RUN_SUMMARY_COLUMNS = (
    "id, created_at, finished_at, filename, status, outcome, vendor_name, invoice_number, currency, "
    "total, po_number, duration_ms, resolution, resolved_at, json_extract(decision, '$.headline') AS headline"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, database_path: Path, seed_path: Path) -> None:
        self._database_path = database_path
        self._seed_path = seed_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    # ----------------------------------------------------------------- setup

    def initialise(self) -> None:
        """Create the tables and load the starting procurement data the first time the app runs."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            if connection.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 0:
                self._load_seed(connection)

    def reset_procurement(self) -> None:
        """Put vendors, purchase orders and the ledger back to their starting state."""
        with self._connect() as connection:
            for table in ("vendors", "purchase_orders", "ledger"):
                connection.execute(f"DELETE FROM {table}")
            self._load_seed(connection)

    def close_open_reviews(self, note: str) -> None:
        """Close runs still waiting for a person, so they don't block the same invoices after a reset."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET resolution = 'closed', resolution_note = ?, resolved_at = ? "
                "WHERE outcome = 'needs_review' AND resolution IS NULL",
                (note, utc_now()),
            )

    def clear_runs(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM runs")

    def _load_seed(self, connection: sqlite3.Connection) -> None:
        seed = json.loads(self._seed_path.read_text(encoding="utf-8"))
        for vendor in (Vendor(**row) for row in seed["vendors"]):
            connection.execute(
                "INSERT INTO vendors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (vendor.id, vendor.name, vendor.tax_id, vendor.bank_name, vendor.bank_account,
                 vendor.bank_ifsc, vendor.phone, vendor.email, vendor.status),
            )
        for po in (PurchaseOrder(**row) for row in seed["purchase_orders"]):
            connection.execute(
                "INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (po.po_number, reference_key(po.po_number), po.vendor_id, po.description, po.currency, po.amount,
                 po.tax_rate_percent, po.status, po.order_date, json.dumps([line.model_dump() for line in po.lines])),
            )
        for entry in (LedgerEntry(**row) for row in seed["ledger"]):
            self._insert_ledger_entry(connection, entry)

    # ----------------------------------------------------------------- procurement data

    def vendors(self) -> list[Vendor]:
        with self._connect() as connection:
            return [Vendor(**dict(row)) for row in connection.execute("SELECT * FROM vendors ORDER BY name")]

    def purchase_order_balances(self, vendor_id: str | None = None, only_open: bool = False) -> list[PoBalance]:
        clauses, parameters = [], []
        if vendor_id:
            clauses.append("vendor_id = ?")
            parameters.append(vendor_id)
        if only_open:
            clauses.append("status = 'open'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(f"SELECT * FROM purchase_orders {where} ORDER BY po_number", parameters).fetchall()
            return [self._balance(connection, row) for row in rows]

    def find_purchase_order(self, reference: str) -> PoBalance | None:
        """Look a PO up the way a person would: ignoring punctuation, spacing and leading zeros."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM purchase_orders WHERE po_key = ?", (reference_key(reference),)
            ).fetchone()
            return self._balance(connection, row) if row else None

    def _balance(self, connection: sqlite3.Connection, po_row: sqlite3.Row) -> PoBalance:
        po = PurchaseOrder(**{**dict(po_row), "lines": json.loads(po_row["lines"])})
        invoices = connection.execute(
            "SELECT * FROM ledger WHERE po_number = ? ORDER BY posted_at, id", (po.po_number,)
        ).fetchall()
        return PoBalance(po=po, invoices=[LedgerEntry(**dict(row)) for row in invoices])

    def ledger(self, vendor_id: str | None = None) -> list[LedgerEntry]:
        query = "SELECT * FROM ledger" + (" WHERE vendor_id = ?" if vendor_id else "") + " ORDER BY posted_at DESC, id DESC"
        with self._connect() as connection:
            rows = connection.execute(query, (vendor_id,) if vendor_id else ()).fetchall()
            return [LedgerEntry(**dict(row)) for row in rows]

    def record_invoice(self, entry: LedgerEntry) -> None:
        with self._connect() as connection:
            self._insert_ledger_entry(connection, entry)

    @staticmethod
    def _insert_ledger_entry(connection: sqlite3.Connection, entry: LedgerEntry) -> None:
        connection.execute(
            "INSERT INTO ledger (vendor_id, invoice_number, invoice_date, pre_tax, total, po_number, posted_at, "
            "recorded_by, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.vendor_id, entry.invoice_number, entry.invoice_date, entry.pre_tax, entry.total,
             entry.po_number, entry.posted_at, entry.recorded_by, entry.run_id),
        )

    def invoices_waiting_for_review(self, vendor_id: str) -> list[QueuedInvoice]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, invoice_key FROM runs WHERE vendor_id = ? AND outcome = 'needs_review' "
                "AND resolution IS NULL AND invoice_key IS NOT NULL",
                (vendor_id,),
            ).fetchall()
            return [QueuedInvoice(run_id=row["id"], invoice_key=row["invoice_key"]) for row in rows]

    # ----------------------------------------------------------------- runs

    def create_run(self, filename: str, file_path: Path, media_type: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO runs (created_at, filename, file_path, media_type) VALUES (?, ?, ?, ?)",
                (utc_now(), filename, str(file_path), media_type),
            )
            return cursor.lastrowid

    def save_progress(self, run_id: int, stages: list[StageProgress]) -> None:
        """Written after every stage, which is what the live run view polls."""
        self.update_run(run_id, stages=[stage.model_dump(mode="json") for stage in stages])

    def update_run(self, run_id: int, **fields: Any) -> None:
        for column in _RUN_JSON_COLUMNS:
            if column in fields and fields[column] is not None:
                fields[column] = json.dumps(fields[column])
        assignments = ", ".join(f"{column} = ?" for column in fields)
        with self._connect() as connection:
            connection.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*fields.values(), run_id))

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = dict(row)
        for column in _RUN_JSON_COLUMNS:
            run[column] = json.loads(run[column]) if run[column] else None
        return run

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(f"SELECT {_RUN_SUMMARY_COLUMNS} FROM runs ORDER BY id DESC").fetchall()
            return [dict(row) for row in rows]

    def count_runs_since(self, iso_timestamp: str) -> int:
        with self._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM runs WHERE created_at >= ?", (iso_timestamp,)).fetchone()[0]
