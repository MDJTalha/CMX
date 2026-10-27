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
    assert ":focus-visible" in css.text
    assert js.status_code == 200
    assert "/api/v1/assessments/evaluate" in js.text
    assert '"Authorization":"Bearer "+token.trim()' in js.text
    assert '$("#api-token").value=""' in js.text


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

def test_frontend_navigation_exposes_accessible_current_and_mobile_state():
    client = TestClient(app)
    page = client.get("/")
    script = client.get("/app.js")
    assert page.status_code == 200
    assert 'aria-controls="sidebar"' in page.text
    assert 'aria-expanded="false"' in page.text
    assert script.status_code == 200
    assert 'setAttribute("aria-current","page")' in script.text
    assert 'e.key==="Escape"' in script.text
    assert 'prefers-reduced-motion: reduce' in script.text

def test_candidate_fields_have_accessible_labels_and_nonnegative_costs():
    client = TestClient(app)
    script = client.get("/app.js")
    assert script.status_code == 200
    for expected in (
        "for=\"candidate-'+id+'-'+name+'\"",
        'aria-label="Supported capability keys"',
        'aria-label="Evidence source URLs (comma separated)"',
        'min="0" step="any"',
    ):
        assert expected in script.text


def test_assessment_uses_an_accessible_ephemeral_credential_field_not_a_prompt():
    client = TestClient(app)
    page = client.get("/")
    script = client.get("/app.js")
    assert page.status_code == 200
    assert 'id="api-token" name="api-token" type="password" autocomplete="off"' in page.text
    assert 'aria-describedby="api-token-help"' in page.text
    assert '$("#api-token").value' in script.text
    assert '$("#api-token").value=""' in script.text
    assert "prompt(" not in script.text


def test_imported_session_counts_and_nested_records_are_validated_before_rendering():
    client = TestClient(app)
    script = client.get("/app.js")
    assert script.status_code == 200
    assert "function safeCount(v)" in script.text
    assert "safeCount(x.result.evaluated_count)" in script.text
    assert "function validSessionRecord(x)" in script.text
    assert "x.result.evaluated_count!==x.result.evaluations.length" in script.text
    assert "Number.isFinite(e.documented_match_score)" in script.text
    assert "c[k]!==undefined&&!strs(c[k])" in script.text
    assert "x.result.evaluations.length>50" in script.text
    assert "x.request.requirements.length>50" in script.text
    assert "Number.isInteger(r.weight)" in script.text
    assert "r.weight<1||r.weight>5" in script.text
    assert "finiteOrNull(e.estimated_first_year_cost_usd)" in script.text
