from pathlib import Path

from fastapi.testclient import TestClient

from cmx.main import app


def test_frontend_homepage_is_served():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "CMX" in response.text
    assert 'id="assessment-form"' in response.text
    assert "/app.css" in response.text
    assert "/app.js" in response.text


def test_frontend_assets_are_served():
    client = TestClient(app)
    css = client.get("/app.css")
    js = client.get("/app.js")
    assert css.status_code == 200
    assert "prefers-reduced-motion" in css.text
    assert js.status_code == 200
    assert "/api/v1/assessments/evaluate" in js.text
    assert '"Authorization":"Bearer "+token.trim()' in js.text
    assert "will not be saved" in js.text


def test_health_and_api_metadata_remain_available_with_frontend():
    client = TestClient(app)
    health = client.get("/health")
    metadata = client.get("/api/v1")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert metadata.status_code == 200
    assert metadata.json()["brand"] == "CMX"


def test_frontend_workspace_views_and_session_controls_are_present():
    client = TestClient(app)
    page = client.get("/")
    script = client.get("/app.js")
    assert page.status_code == 200
    for view_id in ("view-library", "view-templates", "view-settings", "view-trust", "view-results"):
        assert f'id="{view_id}"' in page.text
    for control_id in ("export-session", "import-session", "clear-session", "settings-refresh"):
        assert f'id="{control_id}"' in page.text
    assert script.status_code == 200
    for capability in (
        "function library()",
        "function applyTemplate(",
        "cmx-session-export-v1",
        "Import file exceeds the 5 MB limit",
    ):
        assert capability in script.text


def test_frontend_does_not_claim_unimplemented_discovery_or_server_persistence():
    client = TestClient(app)
    page = client.get("/")
    documentation = Path(__file__).resolve().parents[1] / "docs" / "frontend.md"
    assert page.status_code == 200
    assert "does not discover or independently verify vendors" in page.text
    assert "server-persisted assessment history" in documentation.read_text(encoding="utf-8")
