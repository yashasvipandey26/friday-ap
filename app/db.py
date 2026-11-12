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
from urllib.parse import parse_qsl, unquote


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


def connection_params(url: str) -> dict[str, Any]:
    """Split a postgresql:// URL into separate connection settings.

    People paste database passwords as they are, with characters like # @ ! that a URL would need
    encoded. Splitting on the LAST "@" accepts them either way. Passing the parts separately also
    means a malformed URL can never be echoed, password and all, into an error message or a log.
    """
    url = url.strip().strip('"').strip("'")
    scheme, separator, rest = url.partition("://")
    if not separator or scheme not in ("postgres", "postgresql"):
        raise ValueError("DATABASE_URL must start with postgresql://")
    credentials, at, location = rest.rpartition("@")
    location, _, query = location.partition("?")
    address, _, dbname = location.partition("/")
    host, _, port = address.rpartition(":") if address.count(":") == 1 else (address, "", "")
    user, _, password = credentials.partition(":") if at else ("", "", "")
    params: dict[str, Any] = {
        "host": host, "port": int(port) if port else 5432, "dbname": unquote(dbname) or "postgres",
        "user": unquote(user), "password": unquote(password), "sslmode": "prefer",
    }
    params.update(dict(parse_qsl(query)))
    return {key: value for key, value in params.items() if value not in ("", None)}


def explain_connection_error(error: Exception, params: dict[str, Any]) -> str:
    """Say what is wrong with DATABASE_URL in plain words. The password never appears in the result."""
    detail = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    if params.get("password"):
        detail = detail.replace(params["password"], "***")
    host, user = params.get("host", "?"), params.get("user", "?")
    where = f"host {host}, user {user}"
    lowered = detail.lower()
    if "password authentication failed" in lowered:
        reason = ("The database rejected the password. If you reset it in Supabase, put the new one in "
                  "DATABASE_URL everywhere it is set (your .env and Render).")
    elif "tenant or user not found" in lowered:
        reason = ("Supabase's pooler doesn't know this user. For the Session pooler the user must be "
                  "postgres.<project-ref>, exactly as Supabase > Connect > Session pooler shows it.")
    elif any(text in lowered for text in ("could not translate host", "name or service not known",
                                           "nodename nor servname", "failed to resolve")):
        reason = "The host name in DATABASE_URL doesn't exist. Copy the string again from Supabase > Connect."
    elif host.startswith("db.") and host.endswith(".supabase.co"):
        reason = ("This is Supabase's direct connection, which only works over IPv6, and most hosts (Render "
                  "included) can't reach it. Use the Session pooler string from Supabase > Connect instead.")
    elif "timeout" in lowered or "timed out" in lowered or "unreachable" in lowered or "refused" in lowered:
        reason = ("The database didn't answer. Check that the Supabase project isn't paused and that this is "
                  "the Session pooler string.")
    else:
        reason = "Check DATABASE_URL."
    return f"Couldn't connect to the database ({where}): {reason} The server said: {detail}"


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
        import psycopg
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        params = {**connection_params(url), "connect_timeout": 10}
        # One plain connection first: a pool retries quietly and only ever reports "timed out", which
        # hides whether the password, the user or the address is wrong.
        try:
            psycopg.connect(**params).close()
        except psycopg.Error as error:
            raise ConnectionError(explain_connection_error(error, params)) from None

        # prepare_threshold=None: Supabase's pooler in transaction mode can't keep prepared statements.
        self._pool = ConnectionPool(
            "", min_size=1, max_size=6, open=False, timeout=20,
            kwargs={**params, "row_factory": dict_row, "prepare_threshold": None},
        )
        self._pool.open(wait=True, timeout=20)

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        with self._pool.connection() as connection:  # commits on success, rolls back on an exception
            yield _PostgresTransaction(connection)

    def close(self) -> None:
        self._pool.close()
