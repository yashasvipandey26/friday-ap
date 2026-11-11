"""Settings, read once from the environment at start-up."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .rules import Policy

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    model: str
    company_name: str
    data_dir: Path
    seed_path: Path = PROJECT_ROOT / "data" / "seed.json"
    samples_dir: Path = PROJECT_ROOT / "samples"
    static_dir: Path = PROJECT_ROOT / "static"
    max_upload_bytes: int = 8 * 1024 * 1024
    max_pages: int = 6
    max_runs_per_day: int = 300  # a public demo link should not be able to run up an AI bill
    read_effort: str = "medium"
    policy: Policy = field(default_factory=Policy)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "friday.sqlite3"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        model=os.getenv("FRIDAY_MODEL", "claude-sonnet-5-5"),
        company_name=os.getenv("FRIDAY_COMPANY_NAME", "Meridian Retail"),
        data_dir=Path(os.getenv("FRIDAY_DATA_DIR", PROJECT_ROOT / "var")),
        max_runs_per_day=int(os.getenv("FRIDAY_MAX_RUNS_PER_DAY", "300")),
        read_effort=os.getenv("FRIDAY_READ_EFFORT", "medium"),
        policy=Policy(tolerance_pct=float(os.getenv("FRIDAY_TOLERANCE_PCT", "2.0"))),
    )
