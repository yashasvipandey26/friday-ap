"""The process itself: a list of stages run in order, each recorded as it happens.

Adding a check means writing one method and adding one line to the stage list. The orchestration
(progress, timing, stopping early, surviving a crashed stage) is written once, in `run`.

A run can be started again after the app restarts part-way through it, so every step is safe to
repeat: nothing is recorded until the decision, and recording the same invoice twice is refused.
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field
from datetime import date
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
    PoBalance,
    StageProgress,
    Status,
    Vendor,
    reference_key,
)
from .files import FileStore
from .outbox import Outbox
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
    file_key: str
    filename: str
    media_type: str
    email_id: int | None
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
    def __init__(self, store: Store, reader: InvoiceReader, explainer: Explainer, settings: Settings,
                 files: FileStore, outbox: Outbox) -> None:
        self._store = store
        self._reader = reader
        self._explainer = explainer
        self._settings = settings
        self._files = files
        self._outbox = outbox
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
            run_id=run_id, file_key=record["file_key"], filename=record["filename"],
            media_type=record["media_type"], email_id=record["email_id"], today=date.today(),
        )
        progress = [StageProgress(key=stage.key, title=stage.title, actor=stage.actor) for stage in self._stages]
        progress.append(StageProgress(key="decide", title="Decide and explain", actor="rules"))
        progress.append(StageProgress(key="notify", title="Tell whoever needs to know", actor="system"))
        self._store.update_run(run_id, status="running")

        try:
            self._run_checks(ctx, progress)
            decision, explanation = self._decide(ctx, progress)
            self._notify(ctx, decision, explanation, progress)
            self._save_result(ctx, decision, explanation, progress, started)
        except Exception:
            logger.exception("Run %s failed", run_id)
            self._store.update_run(run_id, status="failed", finished_at=utc_now())

    def _run_checks(self, ctx: RunContext, progress: list[StageProgress]) -> None:
        settled_early = False
        for stage, row in zip(self._stages, progress):  # the last two rows, decide and notify, run after
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
        row = progress[-2]
        row.state = "running"
        self._store.save_progress(ctx.run_id, progress)
        clock = time.perf_counter()

        decision = rules.decide(ctx.findings, self._po_number(ctx))
        if decision.outcome is Outcome.APPROVED and not self._record(ctx):
            # Another copy of this invoice was recorded between the duplicate check and now.
            ctx.findings.append(Finding(
                code="DUPLICATE", status=Status.REJECT, title="Duplicate invoice", notify="vendor",
                message=f"Invoice {ctx.invoice.invoice_number} was recorded by another run a moment ago.",
                action="Don't pay it twice. Let the vendor know the invoice is already recorded.",
            ))
            decision = rules.decide(ctx.findings, self._po_number(ctx))

        try:
            explanation = self._explainer.explain(ctx.invoice, decision)
        except Exception:  # wording is never allowed to block a decision
            logger.exception("Write-up failed on run %s", ctx.run_id)
            explanation = TemplateExplainer(self._settings.company_name).explain(ctx.invoice, decision)

        row.duration_ms = round((time.perf_counter() - clock) * 1000)
        row.state, row.summary = "done", decision.headline
        row.details = {"outcome": decision.outcome.value, "written_by": explanation.written_by}
        return decision, explanation

    def _record(self, ctx: RunContext) -> bool:
        """Record an approved invoice. A run resumed after a restart may find its own entry already there."""
        if self._store.record_invoice(self._ledger_entry(ctx)):
            return True
        existing = self._store.recorded_invoice(ctx.vendor.id, ctx.invoice.invoice_number)
        return existing is not None and existing.run_id == ctx.run_id

    def _notify(self, ctx: RunContext, decision: Decision, explanation: Explanation,
                progress: list[StageProgress]) -> None:
        row = progress[-1]
        row.state = "running"
        self._store.save_progress(ctx.run_id, progress)
        clock = time.perf_counter()
        try:
            messages = self._outbox.notify(
                run_id=ctx.run_id, draft=explanation.message, findings=ctx.findings, vendor=ctx.vendor,
                po=ctx.po_match.balance.po if ctx.po_match and ctx.po_match.balance else None, email_id=ctx.email_id,
            )
            row.findings = [_message_finding(message) for message in messages]
            if messages:
                row.summary = " ".join(f"{finding.title}." for finding in row.findings)
            elif any(finding.code == "BANK_CHANGED" for finding in ctx.findings):
                row.summary = ("Nobody is emailed: the bank details changed, so the reviewer phones the vendor "
                               "on the number already on file instead.")
            else:
                row.summary = "No one needs to be told."
        except Exception as error:  # a mail problem never undoes a decision
            logger.exception("Notifying failed on run %s", ctx.run_id)
            row.summary = f"Couldn't prepare the email ({type(error).__name__}). The decision stands."
        row.duration_ms = round((time.perf_counter() - clock) * 1000)
        row.state = "done"

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
            headline=decision.headline,
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
        data = self._files.load(ctx.file_key)
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
        if invoice.read_by and invoice.read_by != self._settings.model:
            sentences.append(f"The main model was busy, so the backup model ({invoice.read_by}) read it.")
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
        # A run resumed after a restart must not find itself: leave out anything this run already wrote.
        return _from_findings(rules.find_duplicates(
            ctx.invoice, ctx.vendor,
            recorded=[entry for entry in self._store.ledger(ctx.vendor.id) if entry.run_id != ctx.run_id],
            queued=[item for item in self._store.invoices_waiting_for_review(ctx.vendor.id) if item.run_id != ctx.run_id],
            policy=self._policy,
        ))

    def _match_purchase_order(self, ctx: RunContext) -> StageOutput:
        reference = ctx.invoice.po_reference
        quoted = self._store.find_purchase_order(reference) if reference else None
        ctx.po_match = rules.match_purchase_order(
            ctx.invoice, ctx.vendor,
            quoted=_without_run(quoted, ctx.run_id) if quoted else None,
            open_for_vendor=[_without_run(balance, ctx.run_id)
                             for balance in self._store.purchase_order_balances(ctx.vendor.id, only_open=True)],
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


def _without_run(balance: PoBalance, run_id: int) -> PoBalance:
    """The PO as it stood before this run, so a run repeated after a restart doesn't bill against itself."""
    return PoBalance(po=balance.po, invoices=[entry for entry in balance.invoices if entry.run_id != run_id])


def _message_finding(message: dict[str, Any]) -> Finding:
    who = "the vendor" if message["kind"] == "vendor" else "the PO owner"
    address = message["to_address"] or "no address"
    if message["status"] == "sent":
        how = "Replied in the vendor's email thread" if message["in_reply_to"] else "Sent"
        return Finding(code="EMAIL_SENT", status=Status.PASS, title=f"Emailed {who}",
                       message=f"{how} to {address}: “{message['subject']}”.")
    if message["status"] == "held":
        return Finding(code="EMAIL_HELD", status=Status.NOTE, title=f"Didn't email {who}", message=message["reason"])
    if message["status"] == "failed":
        return Finding(code="EMAIL_FAILED", status=Status.NOTE, title=f"Email to {who} didn't go",
                       message=f"{message['reason']} It can be sent again from this page.")
    return Finding(code="EMAIL_DRAFT", status=Status.NOTE, title=f"Draft ready for {who}",
                   message=message["reason"] or f"Addressed to {address}.")


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
        recorded = store.record_invoice(LedgerEntry(
            vendor_id=run["vendor_id"], invoice_number=run["invoice_number"], invoice_date=run["invoice_date"],
            pre_tax=run["pre_tax"] if run["pre_tax"] is not None else run["total"], total=run["total"],
            po_number=run["po_number"], posted_at=utc_now(), recorded_by="reviewer", run_id=run_id,
        ))
        if not recorded:
            raise ResolutionError(f"Invoice {run['invoice_number']} is already recorded, so approving this copy "
                                  "would pay it twice. Reject this one.")

    store.update_run(
        run_id, resolution="approved" if approve else "rejected",
        resolution_note=note.strip() or None, resolved_at=utc_now(),
    )
    return store.get_run(run_id)
