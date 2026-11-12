"""A thin layer over two databases: SQLite on a laptop and in tests, Postgres (Supabase) when deployed.

SQL is written once, with `?` placeholders and portable syntax (`INSERT ... RETURNING`,
`ON CONFLICT DO NOTHING`). The only per-database differences are the column types for ids and
money, which `schema()` fills in, and the placeholder style, which `_postgres_sql` converts.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol, Sequence


class Transaction(Protocol):
    def execute(self, sql: str, params: Sequence[Any] = ()) -> None: ...
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]: ...
    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None: ...


class Database(Protocol):
    dialect: str  # "sqlite" or "postgres"

    def transaction(self) -> Iterator[Transaction]: ...
    def close(self) -> None: ...


def open_database(database_url: str | None, sqlite_path: Path) -> Database:
    if database_url:
        return PostgresDatabase(database_url)
    return SqliteDatabase(sqlite_path)


def schema(dialect: str, template: str) -> list[str]:
    """Fill in the types that differ, and split the script into single statements."""
    types = {
        "sqlite": {"id": "INTEGER PRIMARY KEY AUTOINCREMENT", "money": "REAL"},
        # REAL is single precision in Postgres, which loses paise on large amounts.
        "postgres": {"id": "BIGSERIAL PRIMARY KEY", "money": "DOUBLE PRECISION"},
    }[dialect]
    script = template.replace("{id}", types["id"]).replace("{money}", types["money"])
    return [statement.strip() for statement in script.split(";") if statement.strip()]


# --------------------------------------------------------------------------- SQLite


class _SqliteTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._connection.execute(sql, tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute(sql, tuple(params)).fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        row = self._connection.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None


class SqliteDatabase:
    dialect = "sqlite"

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as tx:
            tx.execute("PRAGMA journal_mode=WAL")  # readers don't wait for the pipeline's writes

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        connection = sqlite3.connect(self._path, timeout=15, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            yield _SqliteTransaction(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- Postgres


def _postgres_sql(sql: str) -> str:
    # psycopg uses %s placeholders, so any literal % must be doubled first.
    return sql.replace("%", "%%").replace("?", "%s")


class _PostgresTransaction:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._connection.execute(_postgres_sql(sql), tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return list(self._connection.execute(_postgres_sql(sql), tuple(params)).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        return self._connection.execute(_postgres_sql(sql), tuple(params)).fetchone()


class PostgresDatabase:
    """A small connection pool, so the pipeline and the polling page don't open a connection per query."""

    dialect = "postgres"

    def __init__(self, url: str) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # prepare_threshold=None: Supabase's pooler in transaction mode can't keep prepared statements.
        self._pool = ConnectionPool(
            url, min_size=1, max_size=6, open=True, timeout=20,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
        )

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        with self._pool.connection() as connection:  # commits on success, rolls back on an exception
            yield _PostgresTransaction(connection)

    def close(self) -> None:
        self._pool.close()
