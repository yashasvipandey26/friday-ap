"""The HTTP layer, with the real pipeline behind it and the AI reader stubbed."""
import pytest
from fastapi.testclient import TestClient

from app.ai import TemplateExplainer
from app.main import create_app
from tests.readings import StubReader


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings, reader=StubReader(), explainer=TemplateExplainer("Meridian Retail")))


def test_a_sample_runs_end_to_end_and_shows_up_on_the_dashboard(client):
    run_id = client.post("/api/runs", data={"sample_id": "kaveri-furniture"}).json()["id"]

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["outcome"] == "approved"
    assert "file_path" not in run
    assert client.get(run["file_url"]).headers["content-type"] == "application/pdf"

    dashboard = client.get("/api/runs").json()
    assert dashboard["stats"]["processed"] == 1 and dashboard["stats"]["approved_untouched"] == 1
    assert dashboard["runs"][0]["headline"] == "Approved for payment"


def test_an_uploaded_file_is_processed_like_a_sample(client, settings):
    pdf = (settings.samples_dir / "02-northwind-october-freight.pdf").read_bytes()

    response = client.post("/api/runs", files={"file": ("02-northwind-october-freight.pdf", pdf, "application/pdf")})

    assert response.status_code == 201
    assert client.get(f"/api/runs/{response.json()['id']}").json()["outcome"] == "needs_review"


def test_a_file_that_is_not_an_invoice_format_is_refused(client):
    response = client.post("/api/runs", files={"file": ("notes.pdf", b"just some text", "application/pdf")})

    assert response.status_code == 415


def test_a_reviewer_can_resolve_a_held_invoice_once(client):
    run_id = client.post("/api/runs", data={"sample_id": "sahyadri-brochures"}).json()["id"]

    first = client.post(f"/api/runs/{run_id}/resolve", json={"approve": True, "note": "Confirmed"})
    second = client.post(f"/api/runs/{run_id}/resolve", json={"approve": False})

    assert first.json()["resolution"] == "approved"
    assert second.status_code == 409


def test_reset_puts_the_procurement_data_back(client):
    client.post("/api/runs", data={"sample_id": "kaveri-furniture"})
    assert len(client.get("/api/procurement").json()["ledger"]) == 4

    client.post("/api/demo/reset", json={"scope": "all"})

    assert len(client.get("/api/procurement").json()["ledger"]) == 3
    assert client.get("/api/runs").json()["runs"] == []


def test_restoring_the_data_closes_open_reviews_so_the_same_invoice_can_be_run_again(client):
    first = client.post("/api/runs", data={"sample_id": "northwind-freight"}).json()["id"]

    client.post("/api/demo/reset", json={"scope": "procurement"})
    second = client.post("/api/runs", data={"sample_id": "northwind-freight"}).json()["id"]

    assert client.get(f"/api/runs/{first}").json()["resolution"] == "closed"
    assert client.get(f"/api/runs/{second}").json()["decision"]["primary_code"] == "BANK_CHANGED"
