"""The web layer: a handful of JSON endpoints and the static front end.

`create_app` takes its AI components as arguments, so tests run the real pipeline
with a stub reader and never call a model. Start it with:

    uvicorn app.main:create_app --factory
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Literal
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ai import Explainer, InvoiceReader, build_ai
from .config import Settings, load_settings
from .pipeline import InvoicePipeline, ResolutionError, resolve_review
from .rules import mask_account
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Decide the file type from its first bytes; a file name or browser-supplied type can say anything.
_FILE_SIGNATURES = {
    b"%PDF": ("application/pdf", ".pdf"),
    b"\x89PNG": ("image/png", ".png"),
    b"\xff\xd8\xff": ("image/jpeg", ".jpg"),
}


class Resolution(BaseModel):
    approve: bool
    note: str = ""


class ResetRequest(BaseModel):
    scope: Literal["procurement", "runs", "all"]


def create_app(
    settings: Settings | None = None,
    reader: InvoiceReader | None = None,
    explainer: Explainer | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.database_path, settings.seed_path)
    store.initialise()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)

    default_reader, default_explainer = build_ai(
        settings.anthropic_api_key, settings.model, settings.company_name, settings.read_effort)
    pipeline = InvoicePipeline(store, reader or default_reader, explainer or default_explainer, settings)
    samples = {sample["id"]: sample for sample in json.loads((settings.samples_dir / "manifest.json").read_text())}

    app = FastAPI(title="Friday", description="Accounts payable invoice processing, from PDF to decision.")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ai_ready": reader is not None or settings.anthropic_api_key is not None,
            "model": settings.model,
            "company": settings.company_name,
            "tolerance_pct": settings.policy.tolerance_pct,
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

    # ------------------------------------------------------------- runs

    @app.post("/api/runs", status_code=201)
    async def start_run(
        background: BackgroundTasks,
        file: UploadFile | None = File(None),
        sample_id: str | None = Form(None),
    ) -> dict[str, int]:
        a_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        if store.count_runs_since(a_day_ago) >= settings.max_runs_per_day:
            raise HTTPException(429, "This demo has reached its daily limit of invoices. Try again tomorrow.")

        if sample_id:
            sample = _require(samples.get(sample_id), "That sample doesn't exist.")
            filename, data = sample["file"], (settings.samples_dir / sample["file"]).read_bytes()
        elif file:
            filename = Path(file.filename or "invoice").name
            data = await file.read(settings.max_upload_bytes + 1)
            if len(data) > settings.max_upload_bytes:
                raise HTTPException(413, f"That file is over {settings.max_upload_bytes // (1024 * 1024)} MB.")
        else:
            raise HTTPException(400, "Attach an invoice file or choose one from the inbox.")

        media_type, extension = _identify_file(data)
        stored_at = settings.upload_dir / f"{uuid4().hex}{extension}"
        stored_at.write_bytes(data)

        run_id = store.create_run(filename, stored_at, media_type)
        background.add_task(pipeline.run, run_id)  # the response returns now; the page polls for progress
        return {"id": run_id}

    @app.get("/api/runs")
    def list_runs() -> dict[str, Any]:
        runs = store.list_runs()
        return {"stats": _summarise(runs), "runs": runs}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int) -> dict[str, Any]:
        run = _require(store.get_run(run_id), "That run doesn't exist.")
        return _public(run)

    @app.get("/api/runs/{run_id}/file")
    def run_file(run_id: int) -> FileResponse:
        run = _require(store.get_run(run_id), "That run doesn't exist.")
        path = Path(run["file_path"])
        if not path.is_file():
            raise HTTPException(404, "The original file is no longer stored.")
        return FileResponse(path, media_type=run["media_type"], filename=run["filename"],
                            content_disposition_type="inline")

    @app.post("/api/runs/{run_id}/resolve")
    def resolve(run_id: int, resolution: Resolution) -> dict[str, Any]:
        try:
            return _public(resolve_review(store, run_id, resolution.approve, resolution.note))
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ResolutionError as error:
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


def _identify_file(data: bytes) -> tuple[str, str]:
    for signature, file_type in _FILE_SIGNATURES.items():
        if data.startswith(signature):
            return file_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise HTTPException(415, "Friday reads PDF, PNG, JPEG and WebP files. This doesn't look like any of those.")


def _public(run: dict[str, Any]) -> dict[str, Any]:
    """The run as the browser sees it: no server paths, plus a link to the original document."""
    visible = {key: value for key, value in run.items() if key not in ("file_path", "invoice_key")}
    return {**visible, "file_url": f"/api/runs/{run['id']}/file"}


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
    }

