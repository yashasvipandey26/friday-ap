from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.ai import TemplateExplainer
from app.config import Settings
from app.db import open_database
from app.files import LocalFileStore
from app.mail import MailError
from app.outbox import Outbox
from app.pipeline import InvoicePipeline
from app.store import Store
from tests.readings import StubReader

# Point this at an empty Postgres database (for example a local Docker one) to run every test against
# Postgres instead of SQLite. All tables in it are dropped before each test.
POSTGRES_URL = os.getenv("MATCHBOOK_TEST_DATABASE_URL")


class FakeMailer:
    """Records what would have been sent; can be told to fail, like a mail server that is down."""

    address = "ap@meridian-demo.com"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_with: str | None = None

    def send(self, **message: Any) -> str:
        if self.fail_with:
            raise MailError(self.fail_with)
        self.sent.append(message)
        return f"<sent-{len(self.sent)}@meridian-demo.com>"


@dataclass
class Desk:
    """The real store and pipeline on a throwaway database, with the AI reader stubbed out."""

    store: Store
    settings: Settings
    reader: StubReader
    files: LocalFileStore
    mailer: FakeMailer = field(default_factory=FakeMailer)
    procurement_email: str | None = None

    @property
    def outbox(self) -> Outbox:
        return Outbox(self.store, self.mailer, procurement_email=self.procurement_email, company_name="Meridian Retail")

    def process(self, filename: str, reading: dict | None = None, email_id: int | None = None) -> dict[str, Any]:
        reader = StubReader({filename: reading}) if reading else self.reader
        pipeline = InvoicePipeline(self.store, reader, TemplateExplainer("Meridian Retail"), self.settings,
                                   self.files, self.outbox)
        key = f"runs/{filename}"
        self.files.save(key, (self.settings.samples_dir / filename).read_bytes(), "application/pdf")
        run_id = self.store.create_run(filename, key, "application/pdf", "email" if email_id else "sample", email_id)
        pipeline.run(run_id)
        return self.store.get_run(run_id)


def _empty_postgres(url: str) -> None:
    import psycopg

    with psycopg.connect(url, autocommit=True) as connection:
        for table in ("outbox", "runs", "emails", "ledger", "purchase_orders", "vendors"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")


@pytest.fixture
def settings(tmp_path) -> Settings:
    if POSTGRES_URL:
        _empty_postgres(POSTGRES_URL)
    return Settings(anthropic_api_key=None, model="stub", company_name="Meridian Retail", data_dir=tmp_path,
                    workers=0, database_url=POSTGRES_URL)


@pytest.fixture
def store(settings: Settings):
    database = open_database(settings.database_url, settings.database_path)
    store = Store(database, settings.seed_path)
    store.initialise()
    yield store
    database.close()


@pytest.fixture
def desk(settings: Settings, store: Store) -> Desk:
    return Desk(store=store, settings=settings, reader=StubReader(), files=LocalFileStore(settings.upload_dir))
