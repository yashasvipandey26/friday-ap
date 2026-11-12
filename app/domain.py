"""The vocabulary of the process: what an invoice, a finding and a decision look like.

Everything else in the app passes these objects around, so this is the file to read first.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, field_validator


def reference_key(reference: str | None) -> str:
    """Reduce an invoice or PO number to a form that survives retyping.

    'KOS/26-27/0874', 'kos-26-27-874' and 'KOS 26 27 0874' all become 'KOS2627874',
    which is how a re-sent invoice with a slightly different number is still caught.
    """
    if not reference:
        return ""
    without_leading_zeros = re.sub(r"\d+", lambda digits: str(int(digits.group())), reference.upper())
    return re.sub(r"[^A-Z0-9]", "", without_leading_zeros)


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _to_number(value: Any) -> Any:
    """Accept '2,83,200.00' or 'Rs. 1,180' as well as plain numbers."""
    if value is None or isinstance(value, (int, float)):
        return value
    number = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value))
    return float(number.group().replace(",", "")) if number else None


# --------------------------------------------------------------------------- the invoice


class LineItem(BaseModel):
    description: str = ""
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None

    _numbers = field_validator("quantity", "unit_price", "amount", mode="before")(_to_number)


class ExtractedInvoice(BaseModel):
    """What the reader found printed on the document. None means 'not on the page'."""

    is_invoice: bool = True  # False for a PO copy, delivery note, statement or anything else attached to an email
    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None  # ISO, YYYY-MM-DD
    currency: str | None = None
    po_reference: str | None = None  # a PO number actually printed on the invoice
    po_hint: str | None = None  # an indirect reference, e.g. "as per your order dated..."
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None  # before tax, only if the invoice states it
    tax_amount: float | None = None
    tax_rate_percent: float | None = None
    tax_treatment: Literal["separate", "embedded", "none", "unclear"] = "unclear"
    total: float | None = None
    amount_paid: float | None = None  # an advance, a part payment, or the whole total when stamped PAID
    bank_account_number: str | None = None
    bank_ifsc: str | None = None
    document_quality: Literal["clear", "degraded", "poor"] = "clear"
    uncertain_fields: list[str] = Field(default_factory=list)
    notes: str | None = None
    read_by: str | None = None  # which model did the reading; set by the reader, not part of the AI's answer

    _text = field_validator(
        "vendor_name", "vendor_tax_id", "invoice_number", "invoice_date", "currency",
        "po_reference", "po_hint", "bank_account_number", "bank_ifsc", "notes",
        mode="before",
    )(_blank_to_none)
    _numbers = field_validator("subtotal", "tax_amount", "tax_rate_percent", "total", "amount_paid",
                               mode="before")(_to_number)

    @field_validator("tax_treatment", "document_quality", mode="before")
    @classmethod
    def _lowercase_choice(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


# --------------------------------------------------------------------------- procurement data


class Vendor(BaseModel):
    id: str
    name: str
    tax_id: str
    bank_name: str
    bank_account: str
    bank_ifsc: str
    phone: str
    email: str
    status: Literal["approved", "blocked"] = "approved"


class PurchaseOrder(BaseModel):
    po_number: str
    vendor_id: str
    description: str
    currency: str
    amount: float  # before tax
    tax_rate_percent: float
    status: Literal["open", "closed"] = "open"
    order_date: str
    lines: list[LineItem] = Field(default_factory=list)
    owner_name: str = ""  # who raised the PO, and so who is asked when an invoice doesn't fit it
    owner_email: str = ""


class LedgerEntry(BaseModel):
    """An invoice that has been accepted and counts against its purchase order."""

    vendor_id: str
    invoice_number: str
    invoice_date: str
    pre_tax: float
    total: float
    po_number: str | None = None
    posted_at: str
    recorded_by: Literal["history", "matchbook", "reviewer"] = "history"
    run_id: int | None = None


class PoBalance(BaseModel):
    """A purchase order plus what has already been billed against it."""

    po: PurchaseOrder
    invoices: list[LedgerEntry] = Field(default_factory=list)

    @computed_field
    @property
    def billed(self) -> float:
        return round(sum(entry.pre_tax for entry in self.invoices), 2)

    @computed_field
    @property
    def remaining(self) -> float:
        return round(self.po.amount - self.billed, 2)


class QueuedInvoice(BaseModel):
    """An invoice that is waiting for a person, so a second copy can be recognised."""

    run_id: int
    invoice_key: str


# --------------------------------------------------------------------------- findings and decisions


class Status(str, Enum):
    PASS = "pass"
    NOTE = "note"  # worth showing, does not change the outcome
    REVIEW = "review"  # a person has to look
    REJECT = "reject"  # cannot be paid as it stands

    @property
    def severity(self) -> int:
        return list(Status).index(self)


class Finding(BaseModel):
    """The result of one check, written so a non-finance person can read it."""

    code: str
    status: Status
    title: str
    message: str
    action: str = ""  # what a person should do about it
    notify: Literal["vendor", "procurement"] | None = None  # who needs a message, if anyone
    halts: bool = False  # true when later checks would be meaningless
    evidence: dict[str, Any] = Field(default_factory=dict)


class Outcome(str, Enum):
    APPROVED = "approved"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class Decision(BaseModel):
    outcome: Outcome
    headline: str
    reason: str
    next_step: str
    primary_code: str | None = None
    notify: Literal["vendor", "procurement"] | None = None
    issues: list[Finding] = Field(default_factory=list)  # everything that was not a clean pass
    checks_passed: int = 0


class DraftMessage(BaseModel):
    to: Literal["vendor", "procurement"]
    subject: str
    body: str


class Explanation(BaseModel):
    summary: str
    message: DraftMessage | None = None
    written_by: Literal["ai", "template"] = "template"


class StageProgress(BaseModel):
    """One row of the live run view."""

    key: str
    title: str
    actor: Literal["system", "ai", "rules"]
    state: Literal["pending", "running", "done", "skipped"] = "pending"
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = None

    @computed_field
    @property
    def result(self) -> Status | None:
        """The worst finding in the stage, which is what the run view colours it by."""
        if self.state != "done":
            return None
        if not self.findings:
            return Status.PASS
        return max((finding.status for finding in self.findings), key=lambda status: status.severity)
