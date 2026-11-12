"""Everything that is remembered: the procurement data the rules check against, the run history,
the emails that came in and the messages that went out.

All SQL lives here and runs on SQLite or Postgres through `db.py`, so the rest of the app never
knows which database it is using.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import Database, Transaction, schema
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
    currency TEXT NOT NULL, amount {money} NOT NULL, tax_rate_percent {money} NOT NULL, status TEXT NOT NULL,
    order_date TEXT NOT NULL, lines TEXT NOT NULL, owner_name TEXT NOT NULL, owner_email TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id {id}, vendor_id TEXT NOT NULL, invoice_number TEXT NOT NULL, invoice_key TEXT NOT NULL,
    invoice_date TEXT NOT NULL, pre_tax {money} NOT NULL, total {money} NOT NULL, po_number TEXT,
    posted_at TEXT NOT NULL, recorded_by TEXT NOT NULL, run_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ledger_one_per_invoice ON ledger (vendor_id, invoice_key);
CREATE TABLE IF NOT EXISTS emails (
    id {id}, message_id TEXT NOT NULL UNIQUE, received_at TEXT NOT NULL, from_address TEXT NOT NULL,
    from_name TEXT NOT NULL, subject TEXT NOT NULL, body_preview TEXT NOT NULL,
    attachment_count INTEGER NOT NULL, can_reply INTEGER NOT NULL, status TEXT NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id {id}, created_at TEXT NOT NULL, finished_at TEXT,
    filename TEXT NOT NULL, file_key TEXT NOT NULL, media_type TEXT NOT NULL,
    source TEXT NOT NULL, email_id INTEGER,
    status TEXT NOT NULL DEFAULT 'queued', stages TEXT NOT NULL DEFAULT '[]',
    invoice TEXT, decision TEXT, explanation TEXT, outcome TEXT, headline TEXT,
    vendor_id TEXT, vendor_name TEXT, invoice_number TEXT, invoice_key TEXT, invoice_date TEXT,
    currency TEXT, pre_tax {money}, total {money}, po_number TEXT, duration_ms INTEGER,
    resolution TEXT, resolution_note TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id {id}, created_at TEXT NOT NULL, run_id INTEGER, email_id INTEGER,
    kind TEXT NOT NULL, to_address TEXT, to_name TEXT, subject TEXT NOT NULL, body TEXT NOT NULL,
    in_reply_to TEXT, status TEXT NOT NULL, reason TEXT, sent_at TEXT, sent_message_id TEXT
)
"""

_RUN_JSON_COLUMNS = ("stages", "invoice", "decision", "explanation")
_RUN_SUMMARY_COLUMNS = (
    "runs.id, runs.created_at, runs.finished_at, runs.filename, runs.source, runs.status, runs.outcome, "
    "runs.vendor_name, runs.invoice_number, runs.currency, runs.total, runs.po_number, runs.duration_ms, "
    "runs.resolution, runs.resolved_at, runs.headline, emails.from_address AS email_from"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, database: Database, seed_path: Path) -> None:
        self._db = database
        self._seed_path = seed_path

    # ----------------------------------------------------------------- setup

    def initialise(self) -> None:
        """Create the tables and load the starting procurement data the first time the app runs."""
        with self._db.transaction() as tx:
            for statement in schema(self._db.dialect, _SCHEMA):
                tx.execute(statement)
            if tx.query_one("SELECT COUNT(*) AS n FROM vendors")["n"] == 0:
                self._load_seed(tx)

    def reset_procurement(self) -> None:
        """Put vendors, purchase orders and the ledger back to their starting state."""
        with self._db.transaction() as tx:
            for table in ("vendors", "purchase_orders", "ledger"):
                tx.execute(f"DELETE FROM {table}")
            self._load_seed(tx)

    def close_open_reviews(self, note: str) -> None:
        """Close runs still waiting for a person, so they don't block the same invoices after a reset."""
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE runs SET resolution = 'closed', resolution_note = ?, resolved_at = ? "
                "WHERE outcome = 'needs_review' AND resolution IS NULL",
                (note, utc_now()),
            )

    def clear_runs(self) -> None:
        """Forget the run history, and with it the emails and messages that belong to it."""
        with self._db.transaction() as tx:
            for table in ("outbox", "runs", "emails"):
                tx.execute(f"DELETE FROM {table}")

    def _load_seed(self, tx: Transaction) -> None:
        seed = json.loads(self._seed_path.read_text(encoding="utf-8"))
        for vendor in (Vendor(**row) for row in seed["vendors"]):
            tx.execute(
                "INSERT INTO vendors (id, name, tax_id, bank_name, bank_account, bank_ifsc, phone, email, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (vendor.id, vendor.name, vendor.tax_id, vendor.bank_name, vendor.bank_account,
                 vendor.bank_ifsc, vendor.phone, vendor.email, vendor.status),
            )
        for po in (PurchaseOrder(**row) for row in seed["purchase_orders"]):
            tx.execute(
                "INSERT INTO purchase_orders (po_number, po_key, vendor_id, description, currency, amount, "
                "tax_rate_percent, status, order_date, lines, owner_name, owner_email) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (po.po_number, reference_key(po.po_number), po.vendor_id, po.description, po.currency, po.amount,
                 po.tax_rate_percent, po.status, po.order_date, json.dumps([line.model_dump() for line in po.lines]),
                 po.owner_name, po.owner_email),
            )
        for entry in (LedgerEntry(**row) for row in seed["ledger"]):
            self._insert_ledger_entry(tx, entry)

    # ----------------------------------------------------------------- procurement data

    def vendors(self) -> list[Vendor]:
        with self._db.transaction() as tx:
            return [Vendor(**row) for row in tx.query("SELECT * FROM vendors ORDER BY name")]

    def vendor(self, vendor_id: str) -> Vendor | None:
        with self._db.transaction() as tx:
            row = tx.query_one("SELECT * FROM vendors WHERE id = ?", (vendor_id,))
        return Vendor(**row) if row else None

    def purchase_order_balances(self, vendor_id: str | None = None, only_open: bool = False) -> list[PoBalance]:
        clauses, parameters = [], []
        if vendor_id:
            clauses.append("vendor_id = ?")
            parameters.append(vendor_id)
        if only_open:
            clauses.append("status = 'open'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db.transaction() as tx:
            rows = tx.query(f"SELECT * FROM purchase_orders {where} ORDER BY po_number", parameters)
            return [self._balance(tx, row) for row in rows]

    def find_purchase_order(self, reference: str) -> PoBalance | None:
        """Look a PO up the way a person would: ignoring punctuation, spacing and leading zeros."""
        with self._db.transaction() as tx:
            row = tx.query_one("SELECT * FROM purchase_orders WHERE po_key = ?", (reference_key(reference),))
            return self._balance(tx, row) if row else None

    @staticmethod
    def _balance(tx: Transaction, po_row: dict[str, Any]) -> PoBalance:
        po = PurchaseOrder(**{**po_row, "lines": json.loads(po_row["lines"])})
        invoices = tx.query("SELECT * FROM ledger WHERE po_number = ? ORDER BY posted_at, id", (po.po_number,))
        return PoBalance(po=po, invoices=[LedgerEntry(**row) for row in invoices])

    def ledger(self, vendor_id: str | None = None) -> list[LedgerEntry]:
        query = "SELECT * FROM ledger" + (" WHERE vendor_id = ?" if vendor_id else "") + " ORDER BY posted_at DESC, id DESC"
        with self._db.transaction() as tx:
            return [LedgerEntry(**row) for row in tx.query(query, (vendor_id,) if vendor_id else ())]

    def record_invoice(self, entry: LedgerEntry) -> bool:
        """Add an invoice to the ledger. False means this vendor's invoice was already recorded."""
        with self._db.transaction() as tx:
            return self._insert_ledger_entry(tx, entry)

    def recorded_invoice(self, vendor_id: str, invoice_number: str) -> LedgerEntry | None:
        with self._db.transaction() as tx:
            row = tx.query_one("SELECT * FROM ledger WHERE vendor_id = ? AND invoice_key = ?",
                               (vendor_id, reference_key(invoice_number)))
        return LedgerEntry(**row) if row else None

    @staticmethod
    def _insert_ledger_entry(tx: Transaction, entry: LedgerEntry) -> bool:
        # The unique index on (vendor, invoice) is the last line of defence against paying twice:
        # even two copies of one invoice processed at the same moment can't both be recorded.
        inserted = tx.query_one(
            "INSERT INTO ledger (vendor_id, invoice_number, invoice_key, invoice_date, pre_tax, total, po_number, "
            "posted_at, recorded_by, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (vendor_id, invoice_key) DO NOTHING RETURNING id",
            (entry.vendor_id, entry.invoice_number, reference_key(entry.invoice_number), entry.invoice_date,
             entry.pre_tax, entry.total, entry.po_number, entry.posted_at, entry.recorded_by, entry.run_id),
        )
        return inserted is not None

    def invoices_waiting_for_review(self, vendor_id: str) -> list[QueuedInvoice]:
        with self._db.transaction() as tx:
            rows = tx.query(
                "SELECT id, invoice_key FROM runs WHERE vendor_id = ? AND outcome = 'needs_review' "
                "AND resolution IS NULL AND invoice_key IS NOT NULL AND invoice_key <> ''",
                (vendor_id,),
            )
        return [QueuedInvoice(run_id=row["id"], invoice_key=row["invoice_key"]) for row in rows]

    # ----------------------------------------------------------------- runs

    def create_run(self, filename: str, file_key: str, media_type: str, source: str,
                   email_id: int | None = None) -> int:
        with self._db.transaction() as tx:
            return tx.query_one(
                "INSERT INTO runs (created_at, filename, file_key, media_type, source, email_id) "
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (utc_now(), filename, file_key, media_type, source, email_id),
            )["id"]

    def save_progress(self, run_id: int, stages: list[StageProgress]) -> None:
        """Written after every stage, which is what the live run view polls."""
        self.update_run(run_id, stages=[stage.model_dump(mode="json") for stage in stages])

    def update_run(self, run_id: int, **fields: Any) -> None:
        for column in _RUN_JSON_COLUMNS:
            if column in fields and fields[column] is not None:
                fields[column] = json.dumps(fields[column])
        self._update("runs", run_id, fields)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._db.transaction() as tx:
            run = tx.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if run is None:
            return None
        for column in _RUN_JSON_COLUMNS:
            run[column] = json.loads(run[column]) if run[column] else None
        return run

    def list_runs(self) -> list[dict[str, Any]]:
        with self._db.transaction() as tx:
            return tx.query(f"SELECT {_RUN_SUMMARY_COLUMNS} FROM runs "
                            "LEFT JOIN emails ON emails.id = runs.email_id ORDER BY runs.id DESC")

    def unfinished_runs(self) -> list[int]:
        """Runs that were queued or half-way through when the app last stopped."""
        with self._db.transaction() as tx:
            return [row["id"] for row in tx.query(
                "SELECT id FROM runs WHERE status IN ('queued', 'running') ORDER BY id")]

    def count_runs_since(self, iso_timestamp: str) -> int:
        with self._db.transaction() as tx:
            return tx.query_one("SELECT COUNT(*) AS n FROM runs WHERE created_at >= ?", (iso_timestamp,))["n"]

    # ----------------------------------------------------------------- emails in

    def claim_email(self, message_id: str, from_address: str, from_name: str, subject: str,
                    body_preview: str, attachment_count: int, can_reply: bool) -> int | None:
        """Record an incoming email and return its id, or None if it has already been taken in.

        An email whose earlier attempt failed or never finished is handed back, so it is retried
        rather than lost; one that was taken in successfully is never processed twice.
        """
        with self._db.transaction() as tx:
            row = tx.query_one(
                "INSERT INTO emails (message_id, received_at, from_address, from_name, subject, body_preview, "
                "attachment_count, can_reply, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'received') "
                "ON CONFLICT (message_id) DO NOTHING RETURNING id",
                (message_id, utc_now(), from_address, from_name, subject, body_preview, attachment_count,
                 int(can_reply)),
            )
            if row:
                return row["id"]
            earlier = tx.query_one("SELECT id, status FROM emails WHERE message_id = ?", (message_id,))
        return earlier["id"] if earlier and earlier["status"] in ("received", "failed") else None

    def update_email(self, email_id: int, **fields: Any) -> None:
        self._update("emails", email_id, fields)

    def get_email(self, email_id: int) -> dict[str, Any] | None:
        with self._db.transaction() as tx:
            return tx.query_one("SELECT * FROM emails WHERE id = ?", (email_id,))

    def recent_emails(self, limit: int = 15) -> list[dict[str, Any]]:
        """The mailbox view: each email with the runs it started and anything sent back."""
        with self._db.transaction() as tx:
            emails = tx.query("SELECT * FROM emails ORDER BY id DESC LIMIT ?", (limit,))
            if not emails:
                return []
            ids = [email["id"] for email in emails]
            marks = ", ".join("?" for _ in ids)
            runs = tx.query(f"SELECT id, email_id, status, outcome, resolution, headline FROM runs "
                            f"WHERE email_id IN ({marks}) ORDER BY id", ids)
            messages = tx.query(f"SELECT id, email_id, kind, status, to_address FROM outbox "
                                f"WHERE email_id IN ({marks}) ORDER BY id", ids)
        for email in emails:
            email["runs"] = [run for run in runs if run["email_id"] == email["id"]]
            email["messages"] = [message for message in messages if message["email_id"] == email["id"]]
        return emails

    # ----------------------------------------------------------------- messages out

    def add_message(self, *, kind: str, subject: str, body: str, status: str, run_id: int | None = None,
                    email_id: int | None = None, to_address: str | None = None, to_name: str | None = None,
                    in_reply_to: str | None = None, reason: str | None = None) -> int:
        with self._db.transaction() as tx:
            return tx.query_one(
                "INSERT INTO outbox (created_at, run_id, email_id, kind, to_address, to_name, subject, body, "
                "in_reply_to, status, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (utc_now(), run_id, email_id, kind, to_address, to_name, subject, body, in_reply_to, status, reason),
            )["id"]

    def update_message(self, message_id: int, **fields: Any) -> None:
        self._update("outbox", message_id, fields)

    def get_message(self, message_id: int) -> dict[str, Any] | None:
        with self._db.transaction() as tx:
            return tx.query_one("SELECT * FROM outbox WHERE id = ?", (message_id,))

    def messages_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self._db.transaction() as tx:
            return tx.query("SELECT * FROM outbox WHERE run_id = ? ORDER BY id", (run_id,))

    # ----------------------------------------------------------------- shared

    def _update(self, table: str, row_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)  # column names come from code, never input
        with self._db.transaction() as tx:
            tx.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", (*fields.values(), row_id))
