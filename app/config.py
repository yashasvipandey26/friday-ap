"""Settings, read once from the environment at start-up."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .rules import Policy

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The model used when FRIDAY_MODEL is not set, per provider.
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "gemini": "gemini-3.8-flash"}


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    model: str
    company_name: str
    data_dir: Path
    provider: str = "anthropic"  # "anthropic" or "gemini"
    google_api_key: str | None = None
    seed_path: Path = PROJECT_ROOT / "data" / "seed.json"
    samples_dir: Path = PROJECT_ROOT / "samples"
    static_dir: Path = PROJECT_ROOT / "static"
    max_upload_bytes: int = 8 * 1024 * 1024
    max_pages: int = 6
    max_runs_per_day: int = 300  # a public demo link should not be able to run up an AI bill
    read_effort: str = "medium"
    policy: Policy = field(default_factory=Policy)

    @property
    def api_key(self) -> str | None:
        """The key for whichever provider was chosen."""
        return self.google_api_key if self.provider == "gemini" else self.anthropic_api_key

    @property
    def database_path(self) -> Path:
        return self.data_dir / "friday.sqlite3"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


def load_settings() -> Settings:
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY") or None
    google_api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or None
    provider = _choose_provider(os.getenv("FRIDAY_AI_PROVIDER", ""), anthropic_api_key, google_api_key)
    return Settings(
        anthropic_api_key=anthropic_api_key,
        google_api_key=google_api_key,
        provider=provider,
        model=os.getenv("FRIDAY_MODEL") or DEFAULT_MODELS[provider],
        company_name=os.getenv("FRIDAY_COMPANY_NAME", "Meridian Retail"),
        data_dir=Path(os.getenv("FRIDAY_DATA_DIR", PROJECT_ROOT / "var")),
        max_runs_per_day=int(os.getenv("FRIDAY_MAX_RUNS_PER_DAY", "300")),
        read_effort=os.getenv("FRIDAY_READ_EFFORT", "medium"),
        policy=Policy(tolerance_pct=float(os.getenv("FRIDAY_TOLERANCE_PCT", "2.0"))),
    )


def _choose_provider(requested: str, anthropic_api_key: str | None, google_api_key: str | None) -> str:
    """Use FRIDAY_AI_PROVIDER when it is set; otherwise whichever provider has a key (Anthropic if both)."""
    requested = requested.strip().lower()
    if requested in ("google", "gemini"):
        return "gemini"
    if requested in ("anthropic", "claude"):
        return "anthropic"
    if requested:
        raise ValueError(f"FRIDAY_AI_PROVIDER must be 'anthropic' or 'gemini', not '{requested}'.")
    return "gemini" if google_api_key and not anthropic_api_key else "anthropic"
