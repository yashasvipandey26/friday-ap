"""The Claude calls, with the network replaced by a fake client.

These guard the request shape: the current Claude models reject `temperature` and forced tool use,
so the code must ask for structured JSON output instead.
"""
import json
from types import SimpleNamespace

import pytest

from app.ai import ClaudeExplainer, ClaudeInvoiceReader, Document, ReaderError, _INVOICE_SCHEMA
from app.domain import Decision, Outcome


class FakeClient:
    def __init__(self, reply: dict | None = None, stop_reason: str = "end_turn") -> None:
        self.requests: list[dict] = []
        self._reply, self._stop_reason = reply, stop_reason
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(request)
        content = [SimpleNamespace(type="thinking", thinking=""),
                   SimpleNamespace(type="text", text=json.dumps(self._reply))]
        return SimpleNamespace(content=content, stop_reason=self._stop_reason)


READ = dict(
    vendor_name="Sahyadri Print Works", vendor_tax_id="27ABXFS6640K1Z2", invoice_number="SPW/1047",
    invoice_date="2026-11-03", currency="INR", po_reference="", po_hint="As per your order dt. 21-10-2026",
    line_items=[dict(description="Brochures", quantity=5000, unit_price=None, amount=101480)],
    subtotal=None, tax_amount=None, tax_rate_percent=18, tax_treatment="Embedded", total=101480,
    bank_account_number="", bank_ifsc="", document_quality="degraded", uncertain_fields=[], notes="",
)
PDF = Document(filename="scan.pdf", media_type="application/pdf", data=b"%PDF-1.4", kind="scanned_pdf", pages=1)


def test_reader_sends_the_file_itself_and_asks_for_structured_output():
    client = FakeClient(READ)

    invoice = ClaudeInvoiceReader(client, "claude-sonnet-5-5").read(PDF)

    request = client.requests[0]
    assert request["messages"][0]["content"][0]["type"] == "document"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in request and "tool_choice" not in request
    assert invoice.po_reference is None and invoice.bank_account_number is None  # "" means not on the page
    assert invoice.tax_treatment == "embedded"  # enum casing is not guaranteed by the API


def test_invoice_schema_stays_inside_the_structured_output_limits():
    def nullable_fields(schema) -> int:
        if isinstance(schema, dict):
            return isinstance(schema.get("type"), list) + sum(nullable_fields(value) for value in schema.values())
        return 0

    assert nullable_fields(_INVOICE_SCHEMA) <= 16  # documented limit on union-typed parameters
    assert set(_INVOICE_SCHEMA["required"]) == set(_INVOICE_SCHEMA["properties"])  # no optional parameters


def test_a_refusal_or_cut_off_answer_is_an_error_not_a_guess():
    with pytest.raises(ReaderError):
        ClaudeInvoiceReader(FakeClient(READ, stop_reason="max_tokens"), "claude-sonnet-5-5").read(PDF)


def test_writer_only_drafts_a_message_when_the_rules_say_someone_must_be_told():
    decision = Decision(outcome=Outcome.NEEDS_REVIEW, headline="Bank details don't match our records",
                        reason="The account differs.", next_step="Call the vendor.", notify=None)
    client = FakeClient(dict(summary="Held: the bank account differs.", message_subject="Hi", message_body="Please confirm"))

    explanation = ClaudeExplainer(client, "claude-sonnet-5-5", "Meridian Retail").explain(None, decision)

    assert explanation.written_by == "ai"
    assert explanation.message is None  # the model offered one, the rules said no
