"""The Claude and Gemini calls, with the network replaced by fake clients.

These guard the request shape: the current Claude models reject `temperature` and forced tool use,
so the code must ask for structured JSON output instead, and Gemini must be given the same schema.
"""
import json
from types import SimpleNamespace

import pytest

from google.genai import errors as genai_errors

from app.ai import (
    AiExplainer, AiInvoiceReader, ClaudeModel, Document, GeminiModel, ReaderError, UnconfiguredReader,
    _INVOICE_SCHEMA, build_ai,
)
from app.config import DEFAULT_MODELS, load_settings
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


class FakeGeminiClient:
    def __init__(self, reply: dict | None = None, finish_reason: str = "STOP", error: Exception | None = None) -> None:
        self.requests: list[dict] = []
        self._reply, self._finish_reason, self._error = reply, finish_reason, error
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, **request):
        self.requests.append(request)
        if self._error:
            raise self._error
        parts = [SimpleNamespace(text="thinking it over", thought=True),
                 SimpleNamespace(text=json.dumps(self._reply), thought=None)]
        candidate = SimpleNamespace(content=SimpleNamespace(parts=parts),
                                    finish_reason=SimpleNamespace(name=self._finish_reason))
        return SimpleNamespace(candidates=[candidate], prompt_feedback=None)


def claude_reader(client) -> AiInvoiceReader:
    return AiInvoiceReader(ClaudeModel(client, "claude-sonnet-5-5"))


def gemini_reader(client) -> AiInvoiceReader:
    return AiInvoiceReader(GeminiModel(client, "gemini-3.8-flash"))


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

    invoice = claude_reader(client).read(PDF)

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
        claude_reader(FakeClient(READ, stop_reason="max_tokens")).read(PDF)


def test_writer_only_drafts_a_message_when_the_rules_say_someone_must_be_told():
    decision = Decision(outcome=Outcome.NEEDS_REVIEW, headline="Bank details don't match our records",
                        reason="The account differs.", next_step="Call the vendor.", notify=None)
    client = FakeClient(dict(summary="Held: the bank account differs.", message_subject="Hi", message_body="Please confirm"))

    explanation = AiExplainer(ClaudeModel(client, "claude-sonnet-5-5"), "Meridian Retail").explain(None, decision)

    assert explanation.written_by == "ai"
    assert explanation.message is None  # the model offered one, the rules said no


# --------------------------------------------------------------------------- Gemini


def test_gemini_reader_sends_the_file_and_the_same_schema():
    client = FakeGeminiClient(READ)

    invoice = gemini_reader(client).read(PDF)

    request = client.requests[0]
    assert request["model"] == "gemini-3.8-flash"
    assert request["contents"][0].inline_data.mime_type == "application/pdf"
    assert request["config"].response_mime_type == "application/json"
    assert request["config"].response_json_schema == _INVOICE_SCHEMA
    assert request["config"].thinking_config.thinking_level.value == "MEDIUM"
    assert invoice.vendor_name == "Sahyadri Print Works"  # the thinking part is not mistaken for the answer
    assert invoice.po_reference is None and invoice.tax_treatment == "embedded"


@pytest.mark.parametrize("finish_reason", ["MAX_TOKENS", "SAFETY"])
def test_gemini_cut_off_or_blocked_answer_is_an_error_not_a_guess(finish_reason):
    with pytest.raises(ReaderError):
        gemini_reader(FakeGeminiClient(READ, finish_reason=finish_reason)).read(PDF)


def test_gemini_bad_key_says_which_setting_to_fix():
    error = genai_errors.ClientError(400, {"error": {"message": "API key not valid.", "status": "INVALID_ARGUMENT",
                                                     "details": [{"reason": "API_KEY_INVALID"}]}})
    with pytest.raises(ReaderError, match="GOOGLE_API_KEY"):
        gemini_reader(FakeGeminiClient(error=error)).read(PDF)


def test_gemini_writer_follows_the_same_rule_about_messages():
    decision = Decision(outcome=Outcome.REJECTED, headline="Duplicate invoice", reason="Already paid.",
                        next_step="Tell the vendor.", notify="vendor")
    client = FakeGeminiClient(dict(summary="Rejected as a duplicate.", message_subject="Already paid",
                                   message_body="Hello, this was paid in October."))

    explanation = AiExplainer(GeminiModel(client, "gemini-3.8-flash"), "Meridian Retail").explain(None, decision)

    assert explanation.written_by == "ai"
    assert explanation.message.to == "vendor" and explanation.message.subject == "Already paid"
    assert client.requests[0]["config"].thinking_config.thinking_level.value == "LOW"


# --------------------------------------------------------------------------- choosing a provider


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("FRIDAY_AI_PROVIDER", "FRIDAY_MODEL", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_only_a_google_key_means_gemini_with_its_default_model(clean_env):
    clean_env.setenv("GOOGLE_API_KEY", "g-key")

    settings = load_settings()

    assert settings.provider == "gemini" and settings.api_key == "g-key"
    assert settings.model == DEFAULT_MODELS["gemini"]


def test_provider_and_model_can_be_chosen_explicitly(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "a-key")
    clean_env.setenv("GEMINI_API_KEY", "g-key")
    clean_env.setenv("FRIDAY_AI_PROVIDER", "Gemini")
    clean_env.setenv("FRIDAY_MODEL", "gemini-4-argon")

    settings = load_settings()

    assert settings.provider == "gemini" and settings.api_key == "g-key" and settings.model == "gemini-4-argon"


def test_with_no_choice_and_both_keys_anthropic_is_kept(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "a-key")
    clean_env.setenv("GOOGLE_API_KEY", "g-key")

    settings = load_settings()

    assert settings.provider == "anthropic" and settings.model == DEFAULT_MODELS["anthropic"]


def test_an_unknown_provider_fails_at_start_up(clean_env):
    clean_env.setenv("FRIDAY_AI_PROVIDER", "openai")
    with pytest.raises(ValueError, match="FRIDAY_AI_PROVIDER"):
        load_settings()


def test_a_missing_key_names_the_setting_for_the_chosen_provider():
    reader, _ = build_ai("gemini", None, "gemini-3.8-flash", "Meridian Retail")

    assert isinstance(reader, UnconfiguredReader)
    with pytest.raises(ReaderError, match="GOOGLE_API_KEY"):
        reader.read(PDF)


def test_a_google_key_builds_a_gemini_reader():
    reader, explainer = build_ai("gemini", "g-key", "gemini-3.8-flash", "Meridian Retail")

    assert isinstance(reader, AiInvoiceReader) and isinstance(reader._model, GeminiModel)
    assert isinstance(explainer, AiExplainer)
