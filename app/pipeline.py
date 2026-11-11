"""The process itself: a list of stages run in order, each recorded as it happens.

Adding a check means writing one method and adding one line to the stage list. The orchestration
(progress, timing, stopping early, surviving a crashed stage) is written once, in `run`.
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader

from . import rules
from .ai import Document, Explainer, InvoiceReader, ReaderError, TemplateExplainer
from .config import Settings
from .domain import (
    Decision,
    Explanation,
    ExtractedInvoice,
    Finding,
    LedgerEntry,
    Outcome,
    StageProgress,
    Status,
    Vendor,
    reference_key,
)
from .rules import PoMatch, format_money
from .store import Store, utc_now

logger = logging.getLogger(__name__)

_TAX_PHRASES = {
    "separate": "Tax is listed separately.",
    "embedded": "Tax is included in the prices.",
    "none": "No tax is charged.",
    "unclear": "It isn't clear whether tax is included.",
}


@dataclass
class RunContext:
    """What the stages learn about one invoice as they go."""

    run_id: int
    document_path: Path
    filename: str
    media_type: str
    today: date
    document: Document | None = None
    invoice: ExtractedInvoice | None = None
    vendor: Vendor | None = None
    po_match: PoMatch | None = None
    findings: list[Finding] = field(default_factory=list)


@dataclass
class StageOutput:
    summary: str
    findings: list[Finding] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False


@dataclass(frozen=True)
class Stage:
    key: str
    title: str
    actor: str  # "system", "ai" or "rules": shown in the run view so it is clear who did what
    run: Callable[[RunContext], StageOutput]


class ResolutionError(Exception):
    """A person tried to resolve a run that can't be resolved that way."""


class InvoicePipeline:
    def __init__(self, store: Store, reader: InvoiceReader, explainer: Explainer, settings: Settings) -> None:
        self._store = store
        self._reader = reader
        self._explainer = explainer
        self._settings = settings
        self._policy = settings.policy
        self._stages = (
            Stage("receive", "Receive the file", "system", self._receive),
            Stage("read", "Read the invoice", "ai", self._read),
            Stage("validate", "Check it is complete and adds up", "rules", self._validate),
            Stage("vendor", "Identify the vendor", "rules", self._identify_vendor),
            Stage("duplicate", "Look for duplicates", "rules", self._look_for_duplicates),
            Stage("match", "Find the purchase order", "rules", self._match_purchase_order),
            Stage("amounts", "Compare amounts with the PO", "rules", self._compare_amounts),
        )

    # ----------------------------------------------------------------- orchestration

    def run(self, run_id: int) -> None:
        started = time.perf_counter()
        record = self._store.get_run(run_id)
        ctx = RunContext(
            run_id=run_id, document_path=Path(record["file_path"]), filename=record["filename"],
            media_type=record["media_type"], today=date.today(),
        )
        progress = [StageProgress(key=stage.key, title=stage.title, actor=stage.actor) for stage in self._stages]
        progress.append(StageProgress(key="decide", title="Decide and explain", actor="rules"))

        try:
            self._run_checks(ctx, progress)
            decision, explanation = self._decide(ctx, progress)
            self._save_result(ctx, decision, explanation, progress, started)
        except Exception:
            logger.exception("Run %s failed", run_id)
            self._store.update_run(run_id, status="failed", finished_at=utc_now())

    def _run_checks(self, ctx: RunContext, progress: list[StageProgress]) -> None:
        settled_early = False
        for stage, row in zip(self._stages, progress):
            if settled_early:
                row.state, row.summary = "skipped", "Not run, because an earlier step already decided the outcome."
                continue

            row.state = "running"
            self._store.save_progress(ctx.run_id, progress)
            clock = time.perf_counter()
            try:
                output = stage.run(ctx)
            except Exception as error:  # one broken check must send the invoice to a person, not lose it
                logger.exception("Stage %s failed on run %s", stage.key, ctx.run_id)
                output = StageOutput(summary="This step hit an unexpected error.", findings=[Finding(
                    code="STEP_FAILED", status=Status.REVIEW, title="A check couldn't be completed", halts=True,
                    message=f"“{stage.title}” failed ({type(error).__name__}), so the remaining checks were not run.",
                    action="Process this invoice by hand and report the error.",
                )])

            row.duration_ms = round((time.perf_counter() - clock) * 1000)
            row.state = "skipped" if output.skipped else "done"
            row.summary, row.findings, row.details = output.summary, output.findings, output.details
            ctx.findings.extend(output.findings)
            settled_early = any(finding.halts for finding in output.findings)

        self._store.save_progress(ctx.run_id, progress)

    def _decide(self, ctx: RunContext, progress: list[StageProgress]) -> tuple[Decision, Explanation]:
        row = progress[-1]
        row.state = "running"
        self._store.save_progress(ctx.run_id, progress)
        clock = time.perf_counter()

        decision = rules.decide(ctx.findings, self._po_number(ctx))
        try:
            explanation = self._explainer.explain(ctx.invoice, decision)
        except Exception:  # wording is never allowed to block a decision
            logger.exception("Write-up failed on run %s", ctx.run_id)
            explanation = TemplateExplainer(self._settings.company_name).explain(ctx.invoice, decision)

        if decision.outcome is Outcome.APPROVED:
            self._store.record_invoice(self._ledger_entry(ctx))

        row.duration_ms = round((time.perf_counter() - clock) * 1000)
        row.state, row.summary = "done", decision.headline
        row.details = {"outcome": decision.outcome.value, "written_by": explanation.written_by}
        return decision, explanation

    def _save_result(self, ctx: RunContext, decision: Decision, explanation: Explanation,
                     progress: list[StageProgress], started: float) -> None:
        invoice = ctx.invoice
        self._store.update_run(
            ctx.run_id,
            status="completed",
            finished_at=utc_now(),
            stages=[stage.model_dump(mode="json") for stage in progress],
            invoice=invoice.model_dump() if invoice else None,
            decision=decision.model_dump(mode="json"),
            explanation=explanation.model_dump(mode="json"),
            outcome=decision.outcome.value,
            vendor_id=ctx.vendor.id if ctx.vendor else None,
            vendor_name=ctx.vendor.name if ctx.vendor else (invoice.vendor_name if invoice else None),
            invoice_number=invoice.invoice_number if invoice else None,
            invoice_key=reference_key(invoice.invoice_number) if invoice else None,
            invoice_date=invoice.invoice_date if invoice else None,
            currency=(invoice.currency if invoice else None) or "INR",
            pre_tax=self._pre_tax(ctx),
            total=invoice.total if invoice else None,
            po_number=self._po_number(ctx),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )

    # ----------------------------------------------------------------- the stages

    def _receive(self, ctx: RunContext) -> StageOutput:
        data = ctx.document_path.read_bytes()
        size = f"{len(data) / 1024:.0f} KB"
        pages, kind = 1, "image"

        if ctx.media_type == "application/pdf":
            try:
                pdf = PdfReader(io.BytesIO(data))
                if pdf.is_encrypted:
                    raise ValueError("password-protected")
                pages = len(pdf.pages)
                text = "".join(page.extract_text() or "" for page in pdf.pages[:3])
            except Exception:
                return StageOutput(summary="The file can't be opened.", findings=[Finding(
                    code="FILE_UNREADABLE", status=Status.REJECT, title="File can't be opened", halts=True,
                    message="The attachment is damaged or password-protected, so there is nothing to read.",
                    action="Ask the vendor to send the invoice again as a normal PDF.", notify="vendor",
                )])
            kind = "text_pdf" if len(text.strip()) >= 40 else "scanned_pdf"

        if pages > self._settings.max_pages:
            return StageOutput(summary=f"{pages} pages is too long for a single invoice.", findings=[Finding(
                code="TOO_LONG", status=Status.REVIEW, title="Doesn't look like one invoice", halts=True,
                message=f"The file has {pages} pages. It may be several invoices in one attachment.",
                action="Split the file into separate invoices and submit them one at a time.",
            )])

        ctx.document = Document(filename=ctx.filename, media_type=ctx.media_type, data=data, kind=kind, pages=pages)
        page_count = f"{pages} page{'s' if pages != 1 else ''}"
        summary = {
            "text_pdf": f"Digital PDF, {page_count}, {size}.",
            "scanned_pdf": f"Scanned PDF, {page_count}, {size}. It has no text layer, so it will be read as an image.",
            "image": f"Image file, {size}. It will be read as an image.",
        }[kind]
        return StageOutput(summary=summary, details={"kind": kind, "pages": pages, "bytes": len(data)})

    def _read(self, ctx: RunContext) -> StageOutput:
        try:
            invoice = self._reader.read(ctx.document)
        except ReaderError as error:
            return StageOutput(summary=str(error), findings=[Finding(
                code="READ_FAILED", status=Status.REVIEW, title="Couldn't read the invoice", halts=True,
                message=f"{error} Nothing was guessed.",
                action="Run it again, or key the invoice in by hand.",
            )])

        ctx.invoice = invoice
        total = format_money(invoice.total, invoice.currency) if invoice.total is not None else "no clear total"
        lines = len(invoice.line_items)
        sentences = [
            f"{invoice.vendor_name or 'An unnamed vendor'}, invoice {invoice.invoice_number or '(no number)'}, "
            f"{lines} line item{'s' if lines != 1 else ''}, {total}.",
            _TAX_PHRASES[invoice.tax_treatment],
            f"It quotes {invoice.po_reference}." if invoice.po_reference else "It doesn't quote a PO number.",
        ]
        if invoice.document_quality != "clear":
            sentences.append("The copy is a readable scan." if invoice.document_quality == "degraded"
                             else "Parts of the page couldn't be read.")
        return StageOutput(summary=" ".join(sentences), details={"invoice": invoice.model_dump()})

    def _validate(self, ctx: RunContext) -> StageOutput:
        return _from_findings(rules.validate_invoice(ctx.invoice, ctx.today, self._policy))

    def _identify_vendor(self, ctx: RunContext) -> StageOutput:
        ctx.vendor, findings = rules.identify_vendor(ctx.invoice, self._store.vendors(), self._policy)
        return _from_findings(findings)

    def _look_for_duplicates(self, ctx: RunContext) -> StageOutput:
        return _from_findings(rules.find_duplicates(
            ctx.invoice, ctx.vendor,
            recorded=self._store.ledger(ctx.vendor.id),
            queued=self._store.invoices_waiting_for_review(ctx.vendor.id),
            policy=self._policy,
        ))

    def _match_purchase_order(self, ctx: RunContext) -> StageOutput:
        reference = ctx.invoice.po_reference
        ctx.po_match = rules.match_purchase_order(
            ctx.invoice, ctx.vendor,
            quoted=self._store.find_purchase_order(reference) if reference else None,
            open_for_vendor=self._store.purchase_order_balances(ctx.vendor.id, only_open=True),
            policy=self._policy,
        )
        details = {
            "method": ctx.po_match.method,
            "candidates": [candidate.model_dump() for candidate in ctx.po_match.candidates],
        }
        return _from_findings(ctx.po_match.findings, details)

    def _compare_amounts(self, ctx: RunContext) -> StageOutput:
        balance = ctx.po_match.balance if ctx.po_match else None
        if balance is None:
            return StageOutput(summary="Not run: there is no purchase order to compare with.", skipped=True)
        details = {"po_amount": balance.po.amount, "billed": balance.billed, "remaining": balance.remaining}
        return _from_findings(rules.compare_amounts(ctx.invoice, balance, self._policy), details)

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _po_number(ctx: RunContext) -> str | None:
        return ctx.po_match.balance.po.po_number if ctx.po_match and ctx.po_match.balance else None

    @staticmethod
    def _pre_tax(ctx: RunContext) -> float | None:
        if ctx.invoice is None:
            return None
        if ctx.po_match and ctx.po_match.balance:
            return rules.pre_tax_amount(ctx.invoice, ctx.po_match.balance.po.tax_rate_percent).amount
        return ctx.invoice.subtotal if ctx.invoice.subtotal is not None else ctx.invoice.total

    def _ledger_entry(self, ctx: RunContext) -> LedgerEntry:
        return LedgerEntry(
            vendor_id=ctx.vendor.id, invoice_number=ctx.invoice.invoice_number,
            invoice_date=ctx.invoice.invoice_date, pre_tax=self._pre_tax(ctx), total=ctx.invoice.total,
            po_number=self._po_number(ctx), posted_at=utc_now(), recorded_by="friday", run_id=ctx.run_id,
        )


def _from_findings(findings: list[Finding], details: dict[str, Any] | None = None) -> StageOutput:
    summary = " ".join(f"{finding.title}." for finding in findings)
    return StageOutput(summary=summary, findings=findings, details=details or {})


def resolve_review(store: Store, run_id: int, approve: bool, note: str = "") -> dict[str, Any]:
    """Record a person's decision on an invoice the process sent for review."""
    run = store.get_run(run_id)
    if run is None:
        raise LookupError(f"Run {run_id} does not exist.")
    if run["outcome"] != Outcome.NEEDS_REVIEW.value or run["resolution"]:
        raise ResolutionError("This invoice isn't waiting for a decision.")

    if approve:
        if not run["vendor_id"]:
            raise ResolutionError("The vendor isn't on the approved list, so this can't be approved here. "
                                  "Onboard the vendor first.")
        if run["total"] is None or not run["invoice_number"] or not run["invoice_date"]:
            raise ResolutionError("Key details couldn't be read, so there is nothing reliable to record. "
                                  "Key the invoice in by hand instead.")
        store.record_invoice(LedgerEntry(
            vendor_id=run["vendor_id"], invoice_number=run["invoice_number"], invoice_date=run["invoice_date"],
            pre_tax=run["pre_tax"] if run["pre_tax"] is not None else run["total"], total=run["total"],
            po_number=run["po_number"], posted_at=utc_now(), recorded_by="reviewer", run_id=run_id,
        ))

    store.update_run(
        run_id, resolution="approved" if approve else "rejected",
        resolution_note=note.strip() or None, resolved_at=utc_now(),
    )
    return store.get_run(run_id)
