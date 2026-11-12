"""Settings, read once from the environment at start-up."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .mail import MailSettings
from .rules import Policy

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The model used when FRIDAY_MODEL is not set, per provider, and the backup tried when it is busy.
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "gemini": "gemini-3.6-flash"}
DEFAULT_FALLBACKS = {"anthropic": None, "gemini": "gemini-3.5-flash-lite"}


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
    fallback_model: str | None = None
    policy: Policy = field(default_factory=Policy)
    workers: int = 2  # invoices checked at the same time; more only helps on a paid AI tier
    # Where things are kept. Without these, a local SQLite file and a local folder are used.
    database_url: str | None = None
    supabase_url: str | None = None
    supabase_service_key: str | None = None
    storage_bucket: str = "invoices"
    # Email. Without a mailbox the app still works; messages wait as drafts.
    mail: MailSettings | None = None
    mail_send_mode: str = "auto"  # "auto" or "manual"
    procurement_email: str | None = None  # receives every PO-owner note, since the sample owners aren't real

    @property
    def api_key(self) -> str | None:
        """The key for whichever provider was chosen."""
        return self.google_api_key if self.provider == "gemini" else self.anthropic_api_key

    @property
    def database_path(self) -> Path:
        return self.data_dir / "friday.db"  # renamed when the schema changed, so an old local file is left alone

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


def load_settings() -> Settings:
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY") or None
    google_api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or None
    provider = _choose_provider(os.getenv("FRIDAY_AI_PROVIDER", ""), anthropic_api_key, google_api_key)
    fallback = (os.getenv("FRIDAY_FALLBACK_MODEL") or "").strip() or DEFAULT_FALLBACKS[provider]
    send_mode = (os.getenv("FRIDAY_MAIL_SEND") or "auto").strip().lower()
    if send_mode not in ("auto", "manual"):
        raise ValueError(f"FRIDAY_MAIL_SEND must be 'auto' or 'manual', not '{send_mode}'.")
    return Settings(
        anthropic_api_key=anthropic_api_key,
        google_api_key=google_api_key,
        provider=provider,
        model=os.getenv("FRIDAY_MODEL") or DEFAULT_MODELS[provider],
        company_name=os.getenv("FRIDAY_COMPANY_NAME", "Meridian Retail"),
        data_dir=Path(os.getenv("FRIDAY_DATA_DIR", PROJECT_ROOT / "var")),
        max_runs_per_day=int(os.getenv("FRIDAY_MAX_RUNS_PER_DAY", "300")),
        read_effort=os.getenv("FRIDAY_READ_EFFORT", "medium"),
        fallback_model=None if fallback in (None, "none") else fallback,
        policy=Policy(tolerance_pct=float(os.getenv("FRIDAY_TOLERANCE_PCT", "2.0"))),
        workers=int(os.getenv("FRIDAY_WORKERS", "2")),
        database_url=os.getenv("DATABASE_URL") or None,
        supabase_url=os.getenv("SUPABASE_URL") or None,
        supabase_service_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY") or None,
        storage_bucket=os.getenv("FRIDAY_STORAGE_BUCKET") or "invoices",
        mail=_mail_settings(),
        mail_send_mode=send_mode,
        procurement_email=(os.getenv("FRIDAY_PROCUREMENT_EMAIL") or "").strip() or None,
    )


def _mail_settings() -> MailSettings | None:
    address = (os.getenv("FRIDAY_MAIL_ADDRESS") or "").strip()
    password = (os.getenv("FRIDAY_MAIL_APP_PASSWORD") or "").replace(" ", "")  # Google shows it in groups of four
    if not (address and password):
        return None
    return MailSettings(
        address=address.lower(),
        app_password=password,
        imap_host=os.getenv("FRIDAY_MAIL_IMAP_HOST") or "imap.gmail.com",
        smtp_host=os.getenv("FRIDAY_MAIL_SMTP_HOST") or "smtp.gmail.com",
        smtp_port=int(os.getenv("FRIDAY_MAIL_SMTP_PORT") or "465"),
        poll_seconds=max(5, int(os.getenv("FRIDAY_MAIL_POLL_SECONDS") or "20")),
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
