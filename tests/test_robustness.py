"""What happens when things go wrong: restarts, races, outages, odd attachments, cloud storage."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import rules
from app.ai import TemplateExplainer
from app.domain import Finding, LedgerEntry, Status
from app.files import SupabaseFileStore
from app.main import create_app
from app.pipeline import InvoicePipeline, ResolutionError, resolve_review
from tests.readings import READINGS, StubReader

HAPPY = "01-kaveri-chairs-and-desks.pdf"
SCAN_NO_PO = "04-sahyadri-brochures-scan.pdf"


def test_a_run_repeated_after_a_restart_does_not_mistake_itself_for_a_duplicate(desk):
    run = desk.process(HAPPY)
    pipeline = InvoicePipeline(desk.store, desk.reader, TemplateExplainer("Meridian Retail"), desk.settings,
                               desk.files, desk.outbox)

    pipeline.run(run["id"])  # as if the app had died after recording but before saving the result

    assert desk.store.get_run(run["id"])["outcome"] == "approved"
    assert desk.store.find_purchase_order("PO-2026-0412").billed == 240000  # recorded once, not twice


def test_two_copies_racing_past_the_duplicate_check_cannot_both_be_recorded(desk, monkeypatch):
    # Another run records this invoice after our duplicate check ran but before we record it.
    not_seen = [Finding(code="NOT_DUPLICATE", status=Status.PASS, title="First time we've seen it", message="")]
    monkeypatch.setattr(rules, "find_duplicates", lambda *args, **kwargs: not_seen)
    desk.store.record_invoice(LedgerEntry(
        vendor_id="V-KAVERI", invoice_number="KOS/26-27/0931", invoice_date="2026-11-04", pre_tax=240000,
        total=283200, posted_at="2026-11-12T10:00:00+00:00", recorded_by="matchbook", run_id=999))

    run = desk.process(HAPPY)

    assert run["outcome"] == "rejected" and run["decision"]["primary_code"] == "DUPLICATE"
    assert len([entry for entry in desk.store.ledger("V-KAVERI") if entry.invoice_number == "KOS/26-27/0931"]) == 1


def test_a_reviewer_cannot_approve_a_copy_of_an_invoice_that_is_already_recorded(desk):
    first = desk.process(SCAN_NO_PO)
    resolve_review(desk.store, first["id"], approve=True)
    desk.store.update_run(first["id"], resolution=None)  # pretend a second copy was also waiting

    with pytest.raises(ResolutionError, match="already recorded"):
        resolve_review(desk.store, first["id"], approve=True)


def test_an_attachment_that_is_not_an_invoice_is_set_aside_without_writing_to_anyone(desk):
    run = desk.process(HAPPY, {"is_invoice": False, "vendor_name": "Kaveri Office Supplies Pvt Ltd",
                               "notes": "This is a delivery challan."})

    assert run["outcome"] == "needs_review"
    assert run["decision"]["primary_code"] == "NOT_AN_INVOICE"
    assert "delivery challan" in run["decision"]["reason"]
    assert desk.store.messages_for_run(run["id"]) == []


# --------------------------------------------------------------------------- through the API


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings, reader=StubReader(), explainer=TemplateExplainer("Meridian Retail")))


def test_runs_interrupted_by_a_restart_are_finished_when_the_app_starts(settings, store):
    (settings.upload_dir / "runs").mkdir(parents=True, exist_ok=True)
    (settings.upload_dir / "runs" / "kaveri.pdf").write_bytes((settings.samples_dir / HAPPY).read_bytes())
    run_id = store.create_run(HAPPY, "runs/kaveri.pdf", "application/pdf", "upload")
    store.update_run(run_id, status="running")  # the app died here

    with TestClient(create_app(settings, reader=StubReader(), explainer=TemplateExplainer("Meridian Retail"))):
        pass  # starting the app is enough

    assert store.get_run(run_id)["outcome"] == "approved"


def test_a_run_stopped_by_an_outage_can_be_run_again(client, settings):
    pdf = (settings.samples_dir / HAPPY).read_bytes()
    first = client.post("/api/runs", files={"file": ("photo-of-invoice.pdf", pdf, "application/pdf")}).json()["id"]
    assert client.get(f"/api/runs/{first}").json()["decision"]["primary_code"] == "READ_FAILED"  # the stub can't read it

    second = client.post(f"/api/runs/{first}/retry")

    assert second.status_code == 201
    assert client.get(f"/api/runs/{first}").json()["resolution"] == "closed"
    assert client.get(f"/api/runs/{second.json()['id']}").json()["filename"] == "photo-of-invoice.pdf"


def test_a_finished_run_is_not_run_again(client):
    run_id = client.post("/api/runs", data={"sample_id": "kaveri-furniture"}).json()["id"]

    assert client.post(f"/api/runs/{run_id}/retry").status_code == 409


def test_the_mailbox_page_works_without_email_set_up(client):
    mailbox = client.get("/api/mailbox").json()

    assert mailbox["enabled"] is False and mailbox["emails"] == []
    assert client.post("/api/mailbox/check").status_code == 409


def test_a_run_shows_what_was_sent_and_where_it_came_from(client):
    run_id = client.post("/api/runs", data={"sample_id": "brightpath-milestone"}).json()["id"]

    run = client.get(f"/api/runs/{run_id}").json()

    assert run["source"] == "sample" and run["email"] is None
    assert run["messages"][0]["kind"] == "procurement"
    assert run["messages"][0]["status"] == "held"  # the sample PO owner's address is a placeholder
    assert "placeholder" in run["messages"][0]["reason"]
    assert "file_key" not in run


# --------------------------------------------------------------------------- cloud file storage


def test_supabase_storage_is_called_the_documented_way():
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("apikey")))
        if request.url.path.endswith("/bucket/invoices"):
            return httpx.Response(400, json={"error": "Bucket not found"})
        if "/object/sign/" in request.url.path:
            return httpx.Response(200, json={"signedURL": "/object/sign/invoices/runs/a.pdf?token=t"})
        if request.method == "GET":
            return httpx.Response(200, content=b"%PDF-1.4")
        return httpx.Response(200, json={"Key": "invoices/runs/a.pdf"})

    files = SupabaseFileStore("https://abc.supabase.co", "sb_secret_abc", "invoices",
                              client=httpx.Client(transport=httpx.MockTransport(respond)))
    files.ensure_bucket()
    files.save("runs/a.pdf", b"%PDF-1.4", "application/pdf")

    assert files.load("runs/a.pdf") == b"%PDF-1.4"
    assert files.link("runs/a.pdf") == "https://abc.supabase.co/storage/v1/object/sign/invoices/runs/a.pdf?token=t"
    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/storage/v1/bucket/invoices"),
        ("POST", "/storage/v1/bucket"),
        ("POST", "/storage/v1/object/invoices/runs/a.pdf"),
        ("GET", "/storage/v1/object/invoices/runs/a.pdf"),
        ("POST", "/storage/v1/object/sign/invoices/runs/a.pdf"),
    ]
    assert all(key == "sb_secret_abc" for _, _, key in calls)


@pytest.mark.parametrize("key, bearer", [("eyJhbGciOiJIUzI1NiJ9.legacy", True), ("sb_secret_new", False)])
def test_both_kinds_of_supabase_key_are_sent_the_way_supabase_expects(key, bearer):
    seen = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, content=b"%PDF")

    SupabaseFileStore("https://abc.supabase.co", key, "invoices",
                      client=httpx.Client(transport=httpx.MockTransport(respond))).load("runs/a.pdf")

    assert seen["apikey"] == key
    assert ("authorization" in seen) is bearer


# --------------------------------------------------------------------------- the database address


def test_a_database_password_pasted_as_is_still_connects_to_the_right_place():
    from app.db import connection_params

    params = connection_params(
        '"postgresql://postgres.abcref:p@ss#w0rd!,x@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres"')

    assert params == {"host": "aws-1-ap-southeast-2.pooler.supabase.com", "port": 5432, "dbname": "postgres",
                      "user": "postgres.abcref", "password": "p@ss#w0rd!,x", "sslmode": "prefer"}


def test_an_encoded_password_and_options_are_understood():
    from app.db import connection_params

    params = connection_params("postgresql://me:p%40ss@127.0.0.1:5544/postgres?sslmode=disable")

    assert params["password"] == "p@ss" and params["port"] == 5544 and params["sslmode"] == "disable"


@pytest.mark.parametrize("server_said, expected", [
    ('FATAL:  password authentication failed for user "postgres"', "rejected the password"),
    ("FATAL:  Tenant or user not found", "postgres.<project-ref>"),
    ('failed to resolve host "aws-9-nowhere.pooler.supabase.com"', "doesn't exist"),
    ("connection timeout expired", "didn't answer"),
])
def test_a_database_that_wont_connect_says_why_without_the_password(server_said, expected):
    from app.db import connection_params, explain_connection_error

    params = connection_params("postgresql://postgres.abcref:s3cret#!@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres")
    message = explain_connection_error(Exception(server_said + " (password s3cret#!)"), params)

    assert expected in message and "s3cret" not in message
    assert "postgres.abcref" in message and "aws-1-ap-southeast-2" in message


def test_the_direct_supabase_address_is_called_out_by_name():
    from app.db import connection_params, explain_connection_error

    params = connection_params("postgresql://postgres:pw@db.abcref.supabase.co:5432/postgres")

    assert "IPv6" in explain_connection_error(Exception("Network is unreachable"), params)
