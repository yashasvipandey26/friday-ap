"""The web layer: a handful of JSON endpoints, the static front end, and the inbox watcher.

`create_app` takes its outside-world components as arguments (AI reader and writer, mailer, file
store), so tests run the real pipeline against stubs and never call a model or a mail server.
Start it with:

    uvicorn app.main:create_app --factory
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ai import API_KEY_NAMES, Explainer, InvoiceReader, build_ai
from .config import Settings, load_settings
from .db import open_database
from .files import FileStore, FileStoreError, LocalFileStore, SupabaseFileStore, identify_file
from .images import ImageError, prepare_image
from .intake import EmailIntake
from .mail import InboxWatcher, Mailer
from .outbox import Outbox
from .pipeline import InvoicePipeline, ResolutionError, resolve_review
from .rules import mask_account
from .store import Store
from .worker import RunQueue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# The outcome a run ends with when it couldn't be read; only those are worth running again.
_RETRYABLE_CODES = {"READ_FAILED", "STEP_FAILED"}


class Resolution(BaseModel):
    approve: bool
    note: str = ""


class ResetRequest(BaseModel):
    scope: Literal["procurement", "runs", "all"]


def create_app(
    settings: Settings | None = None,
    reader: InvoiceReader | None = None,
    explainer: Explainer | None = None,
    mailer: Mailer | None = None,
    files: FileStore | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    database = open_database(settings.database_url, settings.database_path)
    store = Store(database, settings.seed_path)
    store.initialise()

    if files is None:
        if settings.supabase_url and settings.supabase_service_key:
            files = SupabaseFileStore(settings.supabase_url, settings.supabase_service_key, settings.storage_bucket)
            files.ensure_bucket()
        else:
            files = LocalFileStore(settings.upload_dir)
    if mailer is None and settings.mail:
        mailer = Mailer(settings.mail, f"Accounts Payable, {settings.company_name}")

    default_reader, default_explainer = build_ai(
        settings.provider, settings.api_key, settings.model, settings.company_name, settings.read_effort,
        settings.fallback_model)
    outbox = Outbox(store, mailer, send_mode=settings.mail_send_mode, procurement_email=settings.procurement_email,
                    company_name=settings.company_name)
    pipeline = InvoicePipeline(store, reader or default_reader, explainer or default_explainer, settings, files, outbox)
    queue = RunQueue(pipeline.run, settings.workers)
    watcher = None
    if settings.mail and mailer:
        intake = EmailIntake(store, files, queue, outbox, settings.mail.address, settings.max_upload_bytes)
        watcher = InboxWatcher(settings.mail, intake.handle)
    samples = {sample["id"]: sample for sample in json.loads((settings.samples_dir / "manifest.json").read_text())}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        unfinished = store.unfinished_runs()
        if unfinished:  # the app stopped part-way through these; every step is safe to repeat
            logger.info("Resuming runs interrupted by a restart: %s", unfinished)
        for run_id in unfinished:
            store.update_run(run_id, status="queued", stages=[])
            queue.submit(run_id)
        if watcher:
            watcher.start()
        yield
        if watcher:
            watcher.stop()
        queue.shutdown()
        database.close()

    app = FastAPI(title="Zamp AI Matchbook", description="Accounts payable invoice processing, from email to decision.",
                  lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ai_ready": reader is not None or settings.api_key is not None,
            "provider": settings.provider,
            "key_name": API_KEY_NAMES[settings.provider],
            "model": settings.model,
            "fallback_model": settings.fallback_model,
            "company": settings.company_name,
            "tolerance_pct": settings.policy.tolerance_pct,
            "database": database.dialect,
            "storage": "supabase" if isinstance(files, SupabaseFileStore) else "local",
            "mail": _mail_status(),
        }

    def _mail_status() -> dict[str, Any]:
        return {
            "enabled": watcher is not None,
            "address": settings.mail.address if settings.mail else None,
            "send_mode": settings.mail_send_mode if mailer else "off",
            "last_checked": watcher.last_checked if watcher else None,
            "last_error": watcher.last_error if watcher else None,
            "poll_seconds": settings.mail.poll_seconds if settings.mail else None,
        }

    # ------------------------------------------------------------- the inbox of sample invoices

    @app.get("/api/samples")
    def list_samples() -> list[dict[str, Any]]:
        return list(samples.values())

    @app.get("/api/samples/{sample_id}/file")
    def sample_file(sample_id: str) -> FileResponse:
        sample = _require(samples.get(sample_id), "That sample doesn't exist.")
        return FileResponse(settings.samples_dir / sample["file"], media_type="application/pdf",
                            filename=sample["file"], content_disposition_type="inline")

    # ------------------------------------------------------------- the real mailbox

    @app.get("/api/mailbox")
    def mailbox() -> dict[str, Any]:
        return {**_mail_status(), "emails": store.recent_emails()}

    @app.post("/api/mailbox/check", status_code=202)
    def check_mailbox() -> dict[str, str]:
        if watcher is None:
            raise HTTPException(409, "Email isn't set up. Add MATCHBOOK_MAIL_ADDRESS and MATCHBOOK_MAIL_APP_PASSWORD.")
        watcher.check_soon()
        return {"status": "checking"}

    # ------------------------------------------------------------- runs

    @app.post("/api/runs", status_code=201)
    async def start_run(file: UploadFile | None = File(None), sample_id: str | None = Form(None)) -> dict[str, int]:
        a_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        if store.count_runs_since(a_day_ago) >= settings.max_runs_per_day:
            raise HTTPException(429, "This demo has reached its daily limit of invoices. Try again tomorrow.")

        if sample_id:
            sample = _require(samples.get(sample_id), "That sample doesn't exist.")
            filename, data, source = sample["file"], (settings.samples_dir / sample["file"]).read_bytes(), "sample"
        elif file:
            filename, source = (file.filename or "invoice").rsplit("/", 1)[-1], "upload"
            data = await file.read(settings.max_upload_bytes + 1)
            if len(data) > settings.max_upload_bytes:
                raise HTTPException(413, f"That file is over {settings.max_upload_bytes // (1024 * 1024)} MB.")
        else:
            raise HTTPException(400, "Attach an invoice file or choose one from the inbox.")

        file_type = identify_file(data)
        if file_type is None:
            raise HTTPException(415, "Matchbook reads PDFs and photos (JPEG, PNG, WebP, HEIC). This doesn't look like any of those.")
        media_type, extension = file_type
        key = f"runs/{uuid4().hex}{extension}"
        try:
            files.save(key, data, media_type)
        except FileStoreError as error:
            raise HTTPException(502, str(error)) from error

        run_id = store.create_run(filename, key, media_type, source)
        queue.submit(run_id)  # the response returns now; the page polls for progress
        return {"id": run_id}

    @app.get("/api/runs")
    def list_runs() -> dict[str, Any]:
        runs = store.list_runs()
        return {"stats": _summarise(runs), "runs": runs}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int) -> dict[str, Any]:
        run = _require(store.get_run(run_id), "That run doesn't exist.")
        return _public(store, run)

    @app.get("/api/runs/{run_id}/file")
    def run_file(run_id: int) -> Response:
        """The original document, served by the app so it can be shown inside the page."""
        run = _require(store.get_run(run_id), "That run doesn't exist.")
        try:
            data = files.load(run["file_key"])
        except FileStoreError as error:
            raise HTTPException(404, str(error)) from error
        media_type = run["media_type"]
        if media_type == "image/heic":  # most browsers can't show HEIC, so they get a JPEG copy to look at
            try:
                prepared = prepare_image(data, media_type)
                data, media_type = prepared.data, prepared.media_type
            except ImageError:
                pass
        return Response(data, media_type=media_type, headers={
            "Content-Disposition": f'inline; filename="{_ascii(run["filename"])}"', "Cache-Control": "private, max-age=3600"})

    @app.get("/api/runs/{run_id}/stored")
    def stored_file(run_id: int) -> Response:
        """A short-lived link to the copy in cloud storage, or the file itself when it is stored locally."""
        run = _require(store.get_run(run_id), "That run doesn't exist.")
        try:
            link = files.link(run["file_key"])
        except FileStoreError as error:
            raise HTTPException(502, str(error)) from error
        return RedirectResponse(link) if link else run_file(run_id)

    @app.post("/api/runs/{run_id}/resolve")
    def resolve(run_id: int, resolution: Resolution) -> dict[str, Any]:
        try:
            return _public(store, resolve_review(store, run_id, resolution.approve, resolution.note))
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ResolutionError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/runs/{run_id}/retry", status_code=201)
    def retry(run_id: int) -> dict[str, int]:
        """Run the same document again as a new run, after an outage stopped the first one."""
        run = _require(store.get_run(run_id), "That run doesn't exist.")
        primary = (run["decision"] or {}).get("primary_code")
        if run["status"] != "failed" and primary not in _RETRYABLE_CODES:
            raise HTTPException(409, "Only runs stopped by an outage or an error can be run again.")
        new_id = store.create_run(run["filename"], run["file_key"], run["media_type"], run["source"], run["email_id"])
        store.update_run(run_id, resolution="closed", resolution_note=f"Run again as #{new_id}.",
                         resolved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        queue.submit(new_id)
        return {"id": new_id}

    @app.post("/api/messages/{message_id}/send")
    def send_message(message_id: int) -> dict[str, Any]:
        try:
            return outbox.send(message_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    # ------------------------------------------------------------- what the rules check against

    @app.get("/api/procurement")
    def procurement() -> dict[str, Any]:
        vendors = store.vendors()
        names = {vendor.id: vendor.name for vendor in vendors}
        return {
            "tolerance_pct": settings.policy.tolerance_pct,
            "vendors": [
                {"name": vendor.name, "tax_id": vendor.tax_id, "phone": vendor.phone, "status": vendor.status,
                 "bank": f"{vendor.bank_name} {mask_account(vendor.bank_account)}"}
                for vendor in vendors
            ],
            "purchase_orders": [
                {**balance.po.model_dump(exclude={"lines"}), "vendor_name": names.get(balance.po.vendor_id, ""),
                 "billed": balance.billed, "remaining": balance.remaining, "invoice_count": len(balance.invoices)}
                for balance in store.purchase_order_balances()
            ],
            "ledger": [
                {**entry.model_dump(), "vendor_name": names.get(entry.vendor_id, "")} for entry in store.ledger()
            ],
        }

    @app.post("/api/demo/reset")
    def reset(request: ResetRequest) -> dict[str, str]:
        """Rehearsal aid: put the procurement data back, or clear the run history."""
        if request.scope in ("procurement", "all"):
            store.reset_procurement()
            store.close_open_reviews("Closed when the purchase order data was restored.")
        if request.scope in ("runs", "all"):
            store.clear_runs()
        return {"reset": request.scope}

    # ------------------------------------------------------------- the front end

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(settings.static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    return app


def _require(value: Any, message: str) -> Any:
    if value is None:
        raise HTTPException(404, message)
    return value


def _ascii(filename: str) -> str:
    return filename.encode("ascii", "replace").decode().replace('"', "'")


def _public(store: Store, run: dict[str, Any]) -> dict[str, Any]:
    """The run as the browser sees it: no storage keys, plus where it came from and what was sent."""
    visible = {key: value for key, value in run.items() if key not in ("file_key", "invoice_key")}
    email = store.get_email(run["email_id"]) if run["email_id"] else None
    return {
        **visible,
        "file_url": f"/api/runs/{run['id']}/file",
        "stored_url": f"/api/runs/{run['id']}/stored",
        "email": {key: email[key] for key in ("from_address", "from_name", "subject", "received_at")} if email else None,
        "messages": store.messages_for_run(run["id"]),
    }


def _summarise(runs: list[dict[str, Any]]) -> dict[str, Any]:
    finished = [run for run in runs if run["status"] == "completed"]
    untouched = [run for run in finished if run["outcome"] == "approved"]
    approved_by_person = [run for run in finished if run["resolution"] == "approved"]
    durations = [run["duration_ms"] for run in finished if run["duration_ms"] is not None]
    return {
        "processed": len(finished),
        "approved_untouched": len(untouched),
        "waiting_for_person": sum(run["outcome"] == "needs_review" and not run["resolution"] for run in finished),
        "rejected": sum(run["outcome"] == "rejected" or run["resolution"] == "rejected" for run in finished),
        "approved_value": sum(run["total"] or 0 for run in untouched + approved_by_person),
        "median_seconds": round(median(durations) / 1000, 1) if durations else None,
        "by_email": sum(run["source"] == "email" for run in runs),
    }
