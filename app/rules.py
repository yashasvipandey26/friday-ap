"""The rule book.

Pure functions: an invoice and procurement data go in, findings come out. There is no AI,
no database and no clock in here, which is what makes every decision repeatable and testable.
The AI reads the document and writes the explanation; it never gets a vote on the outcome.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field

from .domain import (
    Decision,
    ExtractedInvoice,
    Finding,
    LedgerEntry,
    Outcome,
    PoBalance,
    QueuedInvoice,
    Status,
    Vendor,
    reference_key,
)


@dataclass(frozen=True)
class Policy:
    """The thresholds a finance team would want to tune without touching code."""

    tolerance_pct: float = 2.0  # how far past a PO's balance an invoice may go, as % of the PO
    rounding: float = 1.0  # currency units forgiven when adding things up
    vendor_name_match: float = 0.85  # similarity needed to accept a vendor on name alone
    inferred_po_min_score: float = 0.75  # how good a guessed PO must be to be suggested
    inferred_po_min_lead: float = 0.15  # ...and how far ahead of the runner-up
    stale_after_days: int = 180


# --------------------------------------------------------------------------- formatting helpers

_CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}

_FIELD_LABELS = {
    "vendor_name": "vendor name",
    "vendor_tax_id": "vendor tax ID",
    "invoice_number": "invoice number",
    "invoice_date": "invoice date",
    "po_reference": "PO number",
    "subtotal": "subtotal",
    "tax_amount": "tax amount",
    "total": "total",
    "bank_account_number": "bank account number",
    "bank_ifsc": "bank IFSC code",
}
_REQUIRED_FIELDS = ("vendor_name", "invoice_number", "invoice_date", "total")


def _group_indian(whole: int) -> str:
    """12345678 -> '1,23,45,678' (lakh and crore grouping)."""
    digits = str(whole)
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail])


def format_money(amount: float, currency: str | None = "INR") -> str:
    currency = (currency or "INR").upper()
    whole, fraction = divmod(round(abs(amount) * 100), 100)
    grouped = _group_indian(whole) if currency == "INR" else f"{whole:,}"
    text = _CURRENCY_SYMBOLS.get(currency, f"{currency} ") + grouped + (f".{fraction:02d}" if fraction else "")
    return f"-{text}" if amount < 0 else text


def format_date(iso_date: str) -> str:
    try:
        parsed = date.fromisoformat(iso_date[:10])
    except ValueError:
        return iso_date
    return f"{parsed.day} {parsed.strftime('%b %Y')}"


def mask_account(account_number: str) -> str:
    digits = re.sub(r"\W", "", account_number)
    return f"••••{digits[-4:]}" if len(digits) > 4 else digits


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _alnum(text: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


# --------------------------------------------------------------------------- 1. is the invoice usable?


def validate_invoice(invoice: ExtractedInvoice, today: date, policy: Policy) -> list[Finding]:
    missing = [_FIELD_LABELS[name] for name in _REQUIRED_FIELDS if getattr(invoice, name) is None]
    if missing and invoice.document_quality == "poor":
        return [Finding(
            code="UNREADABLE", status=Status.REVIEW, title="Too hard to read", halts=True,
            message=f"The copy is too poor to read the {_join(missing)}. Nothing was guessed.",
            action="Key it in by hand from the document, or ask the vendor for a clearer copy.",
        )]
    if missing:
        return [Finding(
            code="MISSING_FIELDS", status=Status.REJECT, title="Invoice is incomplete", halts=True,
            message=f"The invoice has no {_join(missing)}, so it can't be recorded or paid.",
            action="Send it back to the vendor and ask for a corrected invoice.",
            notify="vendor", evidence={"missing": missing},
        )]

    findings = [Finding(
        code="COMPLETE", status=Status.PASS, title="Has what we need",
        message="Vendor, invoice number, date and total are all on the invoice.",
    )]

    flagged = _normalise_field_names(invoice.uncertain_fields)
    unsure = [label for name, label in _FIELD_LABELS.items() if name in flagged]
    if unsure:
        findings.append(Finding(
            code="LOW_CONFIDENCE", status=Status.REVIEW, title="Reader was not sure",
            message=f"The reader had trouble with the {_join(unsure)}. Those values may be wrong.",
            action="Check the flagged values against the document before approving.",
        ))

    findings.append(_check_arithmetic(invoice, policy))
    date_finding = _check_date(invoice, today, policy)
    if date_finding:
        findings.append(date_finding)
    return findings


def _normalise_field_names(names: list[str]) -> set[str]:
    return {re.sub(r"[^a-z]+", "_", name.lower()).strip("_") for name in names}


def _check_arithmetic(invoice: ExtractedInvoice, policy: Policy) -> Finding:
    """The reader's own confidence is weak evidence; numbers that reconcile are strong evidence."""
    money = lambda amount: format_money(amount, invoice.currency)  # noqa: E731
    amounts = [item.amount for item in invoice.line_items]
    line_total = sum(amounts) if amounts and all(amount is not None for amount in amounts) else None
    checks_run = 0
    problems: list[str] = []

    def compare(left: float, right: float, description: str) -> None:
        nonlocal checks_run
        checks_run += 1
        if abs(left - right) > policy.rounding:
            problems.append(description)

    if invoice.tax_treatment == "embedded":
        if line_total is not None and invoice.total is not None:
            compare(line_total, invoice.total,
                    f"the line items add up to {money(line_total)} but the total says {money(invoice.total)}")
    else:
        if line_total is not None and invoice.subtotal is not None:
            # Many GST invoices print each line with its own tax added, so the lines add up to the total
            # rather than the subtotal. That is consistent too, as long as subtotal plus tax still equals
            # the total, which the next check confirms.
            lines_include_tax = invoice.total is not None and abs(line_total - invoice.total) <= policy.rounding
            compare(line_total, invoice.total if lines_include_tax else invoice.subtotal,
                    f"the line items add up to {money(line_total)} but the subtotal says {money(invoice.subtotal)}")
        if None not in (invoice.subtotal, invoice.tax_amount, invoice.total):
            compare(invoice.subtotal + invoice.tax_amount, invoice.total,
                    f"subtotal plus tax is {money(invoice.subtotal + invoice.tax_amount)} "
                    f"but the total says {money(invoice.total)}")

    if problems:
        return Finding(
            code="MATH_MISMATCH", status=Status.REVIEW, title="Numbers don't add up",
            message=f"On the invoice, {_join(problems)}.",
            action="Compare with the document. If the invoice itself is wrong, send it back to the vendor.",
        )
    if not checks_run:
        return Finding(
            code="MATH_UNCHECKED", status=Status.NOTE, title="Total can't be cross-checked",
            message="The invoice doesn't break its total down enough to cross-check the arithmetic.",
        )
    return Finding(
        code="MATH_OK", status=Status.PASS, title="Numbers add up",
        message="Line items, tax and total reconcile, so the amounts were read consistently.",
    )


def _check_date(invoice: ExtractedInvoice, today: date, policy: Policy) -> Finding | None:
    try:
        invoice_date = date.fromisoformat(invoice.invoice_date or "")
    except ValueError:
        return Finding(
            code="DATE_UNCLEAR", status=Status.REVIEW, title="Date is unclear",
            message=f"The invoice date “{invoice.invoice_date}” isn't a recognisable date.",
            action="Check the date on the document.",
        )
    if invoice_date > today + timedelta(days=1):  # a day of slack: the server runs on UTC, vendors don't
        return Finding(
            code="FUTURE_DATED", status=Status.REVIEW, title="Dated in the future",
            message=f"The invoice is dated {format_date(invoice.invoice_date)}, which hasn't happened yet.",
            action="Confirm the date with the vendor before recording it.",
        )
    if (today - invoice_date).days > policy.stale_after_days:
        return Finding(
            code="STALE", status=Status.NOTE, title="Old invoice",
            message=f"The invoice is dated {format_date(invoice.invoice_date)}, more than "
                    f"{policy.stale_after_days} days ago.",
        )
    return None


# --------------------------------------------------------------------------- 2. who sent it?

_LEGAL_WORDS = {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "company", "corp", "the", "and"}


def _name_key(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return " ".join(word for word in words if word not in _LEGAL_WORDS)


def identify_vendor(invoice: ExtractedInvoice, vendors: list[Vendor], policy: Policy) -> tuple[Vendor | None, list[Finding]]:
    """Tax ID first (hard to get wrong), then name (easy to spoof, so a mismatched tax ID is flagged)."""
    tax_id = _alnum(invoice.vendor_tax_id)
    vendor = next((candidate for candidate in vendors if tax_id and _alnum(candidate.tax_id) == tax_id), None)
    findings: list[Finding] = []

    if vendor:
        findings.append(Finding(
            code="VENDOR_FOUND", status=Status.PASS, title="Approved vendor",
            message=f"Tax ID {vendor.tax_id} belongs to {vendor.name} on the approved vendor list.",
        ))
    else:
        name_key = _name_key(invoice.vendor_name or "")
        scored = [(SequenceMatcher(None, name_key, _name_key(candidate.name)).ratio(), candidate) for candidate in vendors]
        similarity, closest = max(scored, key=lambda pair: pair[0], default=(0.0, None))
        if closest is None or similarity < policy.vendor_name_match:
            return None, [Finding(
                code="VENDOR_UNKNOWN", status=Status.REVIEW, title="Not an approved vendor", halts=True,
                message=f"“{invoice.vendor_name}” isn't on the approved vendor list, by tax ID or by name.",
                action="Don't pay. Send the vendor through onboarding first, then resubmit the invoice.",
            )]
        vendor = closest
        findings.append(Finding(
            code="VENDOR_FOUND", status=Status.PASS, title="Approved vendor",
            message=f"The name matches {vendor.name} on the approved vendor list.",
        ))
        if tax_id:
            findings.append(Finding(
                code="TAX_ID_MISMATCH", status=Status.REVIEW, title="Tax ID doesn't match our records",
                message=f"The invoice shows tax ID {invoice.vendor_tax_id}, but we hold {vendor.tax_id} for {vendor.name}.",
                action="Check the tax ID on the document. If it really differs, confirm with the vendor before paying.",
            ))

    if vendor.status == "blocked":
        return vendor, [Finding(
            code="VENDOR_BLOCKED", status=Status.REJECT, title="Vendor is blocked", halts=True,
            message=f"{vendor.name} is blocked for payment in the vendor master.",
            action="Don't pay. Raise it with procurement.", notify="procurement",
        )]

    findings.append(check_bank_details(invoice, vendor))
    return vendor, findings


def check_bank_details(invoice: ExtractedInvoice, vendor: Vendor) -> Finding:
    """A changed bank account on an otherwise perfect invoice is the classic payment fraud."""
    on_file = f"{vendor.bank_name} {mask_account(vendor.bank_account)}"
    if not invoice.bank_account_number:
        return Finding(
            code="BANK_NOT_SHOWN", status=Status.NOTE, title="No bank details on the invoice",
            message=f"Payment goes to the account we already hold: {on_file}.",
        )
    same_account = re.sub(r"\D", "", invoice.bank_account_number) == re.sub(r"\D", "", vendor.bank_account)
    same_branch = not invoice.bank_ifsc or _alnum(invoice.bank_ifsc) == _alnum(vendor.bank_ifsc)
    if same_account and same_branch:
        return Finding(
            code="BANK_OK", status=Status.PASS, title="Bank details match",
            message=f"The account on the invoice is the one we hold for {vendor.name} ({on_file}).",
        )
    return Finding(
        code="BANK_CHANGED", status=Status.REVIEW, title="Bank details don't match our records",
        message=f"The invoice asks for payment to account {mask_account(invoice.bank_account_number)}"
                f"{' (' + invoice.bank_ifsc + ')' if invoice.bank_ifsc else ''}, but we hold {on_file} "
                f"for {vendor.name}. A changed account on an otherwise normal invoice is how payment fraud usually looks.",
        action=f"Hold payment. Call {vendor.name} on {vendor.phone}, the number in our vendor master, and confirm "
               f"the new account. Don't use any phone number or email address from this invoice, and don't reply to the "
               f"email it came in: if this is fraud, those reach the fraudster.",
        evidence={"on_invoice": mask_account(invoice.bank_account_number), "on_file": mask_account(vendor.bank_account)},
    )


# --------------------------------------------------------------------------- 3. have we seen it before?


def find_duplicates(
    invoice: ExtractedInvoice,
    vendor: Vendor,
    recorded: list[LedgerEntry],
    queued: list[QueuedInvoice],
    policy: Policy,
) -> list[Finding]:
    key = reference_key(invoice.invoice_number)

    same_number = next((entry for entry in recorded if reference_key(entry.invoice_number) == key), None)
    if same_number:
        retyped = same_number.invoice_number != invoice.invoice_number
        return [Finding(
            code="DUPLICATE", status=Status.REJECT, title="Duplicate invoice", halts=True, notify="vendor",
            message=f"This is invoice {same_number.invoice_number} again. It was recorded on "
                    f"{format_date(same_number.posted_at)} for {format_money(same_number.total, invoice.currency)}"
                    f"{' against ' + same_number.po_number if same_number.po_number else ''}."
                    + (f" The number is written differently here ({invoice.invoice_number}), "
                       f"which is how duplicates usually slip past an exact match." if retyped else ""),
            action="Don't pay it twice. Let the vendor know the invoice is already recorded.",
            evidence={"original": same_number.invoice_number, "recorded_on": same_number.posted_at},
        )]

    in_queue = next((item for item in queued if item.invoice_key == key), None)
    if in_queue:
        return [Finding(
            code="DUPLICATE_IN_QUEUE", status=Status.REJECT, title="Already waiting for review", halts=True,
            message=f"The same invoice was submitted earlier and is waiting for a person as run #{in_queue.run_id}.",
            action=f"Nothing to do with this copy. Resolve run #{in_queue.run_id} instead.",
        )]

    lookalike = next((
        entry for entry in recorded
        if invoice.total is not None
        and entry.invoice_date == invoice.invoice_date
        and abs(entry.total - invoice.total) <= policy.rounding
    ), None)
    if lookalike:
        return [Finding(
            code="POSSIBLE_DUPLICATE", status=Status.REVIEW, title="Looks like a duplicate",
            message=f"{vendor.name} already has invoice {lookalike.invoice_number} recorded for the same amount "
                    f"({format_money(lookalike.total, invoice.currency)}) on the same date. The invoice number is "
                    f"different, so this may be a re-issue.",
            action="Ask the vendor whether this replaces the earlier invoice or is a separate charge.",
            notify="vendor",
        )]

    return [Finding(
        code="NOT_DUPLICATE", status=Status.PASS, title="First time we've seen it",
        message=f"No earlier invoice from {vendor.name} has this number, or the same amount on the same date.",
    )]


# --------------------------------------------------------------------------- 4. which purchase order?


class PoCandidate(BaseModel):
    po_number: str
    description: str
    remaining: float
    amount_fit: float
    wording_fit: float
    score: float


class PoMatch(BaseModel):
    balance: PoBalance | None = None
    method: Literal["quoted", "inferred", "none"] = "none"
    findings: list[Finding] = Field(default_factory=list)
    candidates: list[PoCandidate] = Field(default_factory=list)


class PreTax(NamedTuple):
    amount: float | None
    basis: Literal["stated", "worked_back", "no_tax", "unclear"]


def pre_tax_amount(invoice: ExtractedInvoice, po_tax_rate_percent: float) -> PreTax:
    """POs are raised before tax, so every invoice is brought to a before-tax figure first."""
    if invoice.tax_treatment == "embedded" and invoice.total is not None:
        rate = invoice.tax_rate_percent if invoice.tax_rate_percent is not None else po_tax_rate_percent
        return PreTax(round(invoice.total / (1 + rate / 100), 2), "worked_back")
    if invoice.subtotal is not None:
        return PreTax(invoice.subtotal, "stated")
    if invoice.total is not None and invoice.tax_amount is not None:
        return PreTax(round(invoice.total - invoice.tax_amount, 2), "worked_back")
    if invoice.total is not None and invoice.tax_treatment == "none":
        return PreTax(invoice.total, "no_tax")
    return PreTax(invoice.total, "unclear")


def match_purchase_order(
    invoice: ExtractedInvoice,
    vendor: Vendor,
    quoted: PoBalance | None,
    open_for_vendor: list[PoBalance],
    policy: Policy,
) -> PoMatch:
    """Use the PO number if the invoice prints one; otherwise work out the likeliest PO and say so."""
    findings: list[Finding] = []

    if invoice.po_reference and quoted:
        po = quoted.po
        if po.vendor_id != vendor.id:
            return PoMatch(findings=[Finding(
                code="PO_WRONG_VENDOR", status=Status.REVIEW, title="PO belongs to another vendor",
                message=f"The invoice quotes {po.po_number}, but that PO was raised for a different vendor, not {vendor.name}.",
                action="Ask the requester which purchase order this invoice belongs to.", notify="procurement",
            )])
        if po.status == "closed":
            return PoMatch(findings=[Finding(
                code="PO_CLOSED", status=Status.REVIEW, title="PO is closed",
                message=f"The invoice quotes {po.po_number}, which is closed: it has already been billed in full.",
                action="Ask the PO owner whether this is extra work that needs a new PO.", notify="procurement",
            )])
        return PoMatch(balance=quoted, method="quoted", findings=[Finding(
            code="PO_FOUND", status=Status.PASS, title="PO number on the invoice",
            message=f"The invoice quotes {po.po_number} ({po.description}), which is open and was raised for {vendor.name}.",
        )])

    if invoice.po_reference:
        findings.append(Finding(
            code="PO_REF_UNKNOWN", status=Status.NOTE, title="Quoted PO doesn't exist",
            message=f"The invoice quotes “{invoice.po_reference}”, but there is no purchase order with that number.",
        ))

    candidates = sorted(
        (_score_candidate(invoice, balance) for balance in open_for_vendor if balance.remaining > 0),
        key=lambda candidate: candidate.score, reverse=True,
    )
    if not candidates:
        findings.append(Finding(
            code="PO_NOT_FOUND", status=Status.REVIEW, title="No purchase order found",
            message=f"There is no usable PO number on the invoice and {vendor.name} has no open purchase orders.",
            action="Ask the requester whether a PO exists. Without one this needs separate approval.",
            notify="procurement",
        ))
        return PoMatch(findings=findings)

    best = candidates[0]
    lead = best.score - (candidates[1].score if len(candidates) > 1 else 0.0)
    hint = f" The invoice only says “{invoice.po_hint}”." if invoice.po_hint else ""
    if best.score >= policy.inferred_po_min_score and lead >= policy.inferred_po_min_lead:
        runner_up = f" The next closest, {candidates[1].po_number}, scores {candidates[1].score:.0%}." if len(candidates) > 1 else ""
        findings.append(Finding(
            code="PO_INFERRED", status=Status.REVIEW, title="No PO number, best match needs confirming",
            message=f"There is no PO number on the invoice.{hint} Of the open purchase orders for {vendor.name}, "
                    f"{best.po_number} ({best.description}) is the best fit at {best.score:.0%}: "
                    f"amount fit {best.amount_fit:.0%}, wording fit {best.wording_fit:.0%}.{runner_up}",
            action=f"Confirm that this invoice is for {best.po_number}. Approving records it against that PO. "
                   f"A guessed PO is never approved without a person, however good the match.",
            evidence={"suggested": best.po_number},
        ))
        chosen = next(balance for balance in open_for_vendor if balance.po.po_number == best.po_number)
        return PoMatch(balance=chosen, method="inferred", findings=findings, candidates=candidates)

    findings.append(Finding(
        code="PO_AMBIGUOUS", status=Status.REVIEW, title="Can't tell which PO this is for",
        message=f"There is no PO number on the invoice.{hint} {vendor.name} has "
                f"{_count(len(candidates), 'open purchase order')} and none is a clear match "
                f"(closest: {best.po_number} at {best.score:.0%}).",
        action="Ask the requester which purchase order this invoice belongs to.", notify="procurement",
    ))
    return PoMatch(findings=findings, candidates=candidates)


_FILLER_WORDS = {"and", "the", "for", "with", "per", "nos", "inr", "incl", "including", "supply", "charges"}


def _words(text: str) -> set[str]:
    text = re.sub(r"(?<=\d),(?=\d)", "", text.lower())  # 5,000 -> 5000
    words = re.findall(r"[a-z0-9]+", text)
    return {word.rstrip("s") if len(word) > 3 else word for word in words if len(word) > 2 and word not in _FILLER_WORDS}


def _score_candidate(invoice: ExtractedInvoice, balance: PoBalance) -> PoCandidate:
    """60% how well the amount fits what is left on the PO, 40% how much the wording overlaps."""
    po = balance.po
    amount = pre_tax_amount(invoice, po.tax_rate_percent).amount
    amount_fit = 0.0 if amount is None else max(0.0, 1 - abs(amount - balance.remaining) / balance.remaining)

    invoice_words = _words(" ".join(item.description for item in invoice.line_items))
    po_words = _words(po.description + " " + " ".join(line.description for line in po.lines))
    smaller = min(len(invoice_words), len(po_words))
    wording_fit = len(invoice_words & po_words) / smaller if smaller else 0.0

    return PoCandidate(
        po_number=po.po_number, description=po.description, remaining=balance.remaining,
        amount_fit=round(amount_fit, 3), wording_fit=round(wording_fit, 3),
        score=round(0.6 * amount_fit + 0.4 * wording_fit, 3),
    )


# --------------------------------------------------------------------------- 5. do the amounts fit?


def compare_amounts(invoice: ExtractedInvoice, balance: PoBalance, policy: Policy) -> list[Finding]:
    po = balance.po
    money = lambda amount: format_money(amount, po.currency)  # noqa: E731

    if invoice.currency and invoice.currency.upper() != po.currency.upper():
        return [Finding(
            code="CURRENCY_MISMATCH", status=Status.REVIEW, title="Different currency from the PO",
            message=f"The invoice is in {invoice.currency.upper()} but {po.po_number} was raised in {po.currency}.",
            action="Confirm the currency with the vendor before comparing amounts.", notify="vendor",
        )]

    pre_tax = pre_tax_amount(invoice, po.tax_rate_percent)
    findings: list[Finding] = []
    if pre_tax.basis == "unclear":
        findings.append(Finding(
            code="TAX_UNCLEAR", status=Status.REVIEW, title="Can't tell if tax is included",
            message=f"The invoice doesn't say whether {money(invoice.total)} includes tax, so the comparison "
                    f"with the PO below may be off by the tax amount.",
            action="Check the document, or ask the vendor for a tax breakdown.", notify="vendor",
        ))
    elif invoice.tax_treatment == "embedded":
        rate = invoice.tax_rate_percent if invoice.tax_rate_percent is not None else po.tax_rate_percent
        findings.append(Finding(
            code="TAX_EMBEDDED", status=Status.NOTE, title="Tax is included in the total",
            message=f"The invoice shows one tax-inclusive figure. Worked back at {rate:g}%, "
                    f"{money(invoice.total)} is {money(pre_tax.amount)} before tax, which is what the PO is compared with.",
        ))

    findings.append(_check_against_balance(pre_tax.amount, balance, policy))
    tax_finding = _check_tax_rate(invoice, balance, policy)
    if tax_finding:
        findings.append(tax_finding)
    return findings


def _check_against_balance(amount: float, balance: PoBalance, policy: Policy) -> Finding:
    """Compare with what is LEFT on the PO, not the PO total: that is what catches split billing."""
    po = balance.po
    money = lambda value: format_money(value, po.currency)  # noqa: E731
    overage = round(amount - balance.remaining, 2)
    allowed = po.amount * policy.tolerance_pct / 100
    overage_pct = overage / po.amount * 100

    if overage <= policy.rounding:
        left_after = round(balance.remaining - amount, 2)
        if left_after <= policy.rounding:
            message = f"{money(amount)} before tax is exactly what is left on {po.po_number}."
        else:
            message = (f"{money(amount)} before tax fits within the {money(balance.remaining)} left on "
                       f"{po.po_number}; {money(left_after)} will remain after this invoice.")
        return Finding(code="WITHIN_PO", status=Status.PASS, title="Within the purchase order", message=message)

    if overage <= allowed:
        return Finding(
            code="WITHIN_TOLERANCE", status=Status.NOTE, title="Slightly over, inside tolerance",
            message=f"{money(amount)} before tax is {money(overage)} more than is left on {po.po_number}. "
                    f"That is {overage_pct:.1f}% of the PO, inside the {policy.tolerance_pct:g}% tolerance.",
        )

    if balance.invoices:
        earlier = ", ".join(entry.invoice_number for entry in balance.invoices)
        if amount <= po.amount + policy.rounding:
            opening = (f"On its own this invoice ({money(amount)} before tax) fits inside the {money(po.amount)} "
                       f"purchase order. But ")
        else:
            opening = f"This invoice is {money(amount)} before tax, and "
        message = (f"{opening}{money(balance.billed)} has already been billed on "
                   f"{_count(len(balance.invoices), 'earlier invoice')} ({earlier}), leaving {money(balance.remaining)}. This one goes over by "
                   f"{money(overage)}, which is {overage_pct:.1f}% of the PO; the tolerance is {policy.tolerance_pct:g}%.")
    else:
        message = (f"The invoice is {money(amount)} before tax but {po.po_number} is for {money(po.amount)}. "
                   f"That is {money(overage)} over ({overage_pct:.1f}% of the PO); the tolerance is {policy.tolerance_pct:g}%.")
    return Finding(
        code="OVER_PO", status=Status.REVIEW, title="Goes over the purchase order", message=message, notify="procurement",
        action=f"Ask the PO owner to either raise {po.po_number} by {money(overage)} or confirm that only "
               f"{money(max(balance.remaining, 0))} should be paid. Nothing is paid until one of those happens.",
        evidence={"overage": overage, "remaining": balance.remaining, "billed": balance.billed},
    )


def _check_tax_rate(invoice: ExtractedInvoice, balance: PoBalance, policy: Policy) -> Finding | None:
    po = balance.po
    if invoice.tax_treatment != "separate" or invoice.tax_amount is None or invoice.subtotal is None:
        return None
    expected = round(invoice.subtotal * po.tax_rate_percent / 100, 2)
    if abs(invoice.tax_amount - expected) <= policy.rounding:
        return Finding(
            code="TAX_OK", status=Status.PASS, title="Tax rate is right",
            message=f"Tax of {format_money(invoice.tax_amount, po.currency)} is {po.tax_rate_percent:g}% of the "
                    f"subtotal, as the PO expects.",
        )
    return Finding(
        code="TAX_RATE", status=Status.REVIEW, title="Tax doesn't match the PO rate", notify="vendor",
        message=f"The invoice charges {format_money(invoice.tax_amount, po.currency)} tax, but "
                f"{po.tax_rate_percent:g}% of the subtotal is {format_money(expected, po.currency)}.",
        action="Check the tax rate with the vendor before paying.",
    )


# --------------------------------------------------------------------------- 6. the decision

# When several things need a person, this decides which one leads the explanation.
_REVIEW_PRIORITY = (
    "BANK_CHANGED", "VENDOR_UNKNOWN", "TAX_ID_MISMATCH", "POSSIBLE_DUPLICATE", "OVER_PO",
    "PO_WRONG_VENDOR", "PO_CLOSED", "PO_AMBIGUOUS", "PO_NOT_FOUND", "PO_INFERRED",
    "CURRENCY_MISMATCH", "TAX_RATE", "TAX_UNCLEAR", "MATH_MISMATCH", "LOW_CONFIDENCE",
)


def decide(findings: list[Finding], po_number: str | None = None) -> Decision:
    """Any reject wins; otherwise any review wins; otherwise approve. Nothing else can approve an invoice."""
    rejects = [finding for finding in findings if finding.status is Status.REJECT]
    reviews = sorted(
        (finding for finding in findings if finding.status is Status.REVIEW),
        key=lambda finding: _REVIEW_PRIORITY.index(finding.code) if finding.code in _REVIEW_PRIORITY else len(_REVIEW_PRIORITY),
    )
    checks_passed = sum(finding.status is Status.PASS for finding in findings)

    if not rejects and not reviews:
        return Decision(
            outcome=Outcome.APPROVED,
            headline="Approved for payment",
            reason="The vendor is approved, the bank details match our records, the purchase order has room "
                   "for it and the numbers add up.",
            next_step=f"Nothing to do. It is recorded against {po_number} and queued for the next payment run.",
            checks_passed=checks_passed,
        )

    issues = rejects + reviews
    primary = issues[0]
    return Decision(
        outcome=Outcome.REJECTED if rejects else Outcome.NEEDS_REVIEW,
        headline=primary.title,
        reason=primary.message,
        next_step=primary.action,
        primary_code=primary.code,
        notify=primary.notify,
        issues=issues,
        checks_passed=checks_passed,
    )
