from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.ai import TemplateExplainer
from app.config import Settings
from app.pipeline import InvoicePipeline
from app.store import Store
from tests.readings import StubReader


@dataclass
class Desk:
    """The real store and pipeline on a throwaway database, with the AI reader stubbed out."""

    store: Store
    settings: Settings
    reader: StubReader

    def process(self, filename: str, reading: dict | None = None) -> dict[str, Any]:
        reader = StubReader({filename: reading}) if reading else self.reader
        pipeline = InvoicePipeline(self.store, reader, TemplateExplainer("Meridian Retail"), self.settings)
        run_id = self.store.create_run(filename, self.settings.samples_dir / filename, "application/pdf")
        pipeline.run(run_id)
        return self.store.get_run(run_id)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(anthropic_api_key=None, model="stub", company_name="Meridian Retail", data_dir=tmp_path)


@pytest.fixture
def desk(settings: Settings) -> Desk:
    store = Store(settings.database_path, settings.seed_path)
    store.initialise()
    return Desk(store=store, settings=settings, reader=StubReader())
