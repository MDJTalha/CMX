import time

from fastapi.testclient import TestClient

from cmx.main import app
from cmx.service_auth import SQLiteCredentialStore

client = TestClient(app)


def service_headers(tmp_path, monkeypatch, permissions=("assessment:write",)):
    database_path = tmp_path / "cmx-auth.sqlite3"
    monkeypatch.setenv("CMX_AUTH_DB_PATH", str(database_path))
    issued = SQLiteCredentialStore(database_path).issue(
        subject_id="svc_test",
        tenant_id="tenant_test",
        permissions=list(permissions),
        expires_in_seconds=3600,
        now_epoch=int(time.time()),
    )
    return {"Authorization": f"Bearer {issued.token}"}


def assessment_payload():
    return {
        "problem_statement": "Reduce manual enquiry processing.",
        "desired_outcome": "Reduce handling time.",
        "region": "global",
        "max_first_year_cost_usd": 1000,
        "requirements": [
            {
                "key": "email_integration",
                "label": "Email integration",
                "mandatory": True,
                "weight": 5,
            }
        ],
        "candidates": [
            {
                "id": "demo_solution",
                "name": "Demo solution",
                "category": "automation",
                "capability_keys": ["email_integration"],
                "supported_regions": ["global"],
                "setup_cost_usd": 100,
                "monthly_cost_usd": 20,
                "pricing_status": "verified",
                "evidence_status": "verified",
                "source_urls": ["https://example.com/product"],
            }
        ],
    }


def test_health_endpoint_reports_service_identity():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "cmx-decision-engine"


def test_api_metadata_identifies_cmx_and_authentication_mode():
    response = client.get("/api/v1")
    assert response.status_code == 200
    assert response.json()["brand"] == "CMX"
    assert response.json()["meaning"] == "Customer & Market Experience"
    assert response.json()["assessment_authentication"] == "Bearer service credential"


def test_evaluation_endpoint_accepts_authorized_structured_request(tmp_path, monkeypatch):
    headers = service_headers(tmp_path, monkeypatch)
    response = client.post(
        "/api/v1/assessments/evaluate",
        json=assessment_payload(),
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "recommendation_ready"
    assert body["recommended_candidate_id"] == "demo_solution"


def test_evaluation_endpoint_rejects_missing_credentials():
    response = client.post(
        "/api/v1/assessments/evaluate",
        json=assessment_payload(),
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_evaluation_endpoint_rejects_invalid_service_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CMX_AUTH_DB_PATH", str(tmp_path / "cmx-auth.sqlite3"))
    response = client.post(
        "/api/v1/assessments/evaluate",
        json=assessment_payload(),
        headers={"Authorization": "Bearer cmx_live_ABCDEFGHIJKL." + ("A" * 43)},
    )
    assert response.status_code == 401
    assert "credential" in response.json()["detail"].lower()
    assert "ABCDEFGHIJKL" not in response.text


def test_evaluation_endpoint_enforces_server_side_permission(tmp_path, monkeypatch):
    headers = service_headers(tmp_path, monkeypatch, permissions=("assessment:read",))
    response = client.post(
        "/api/v1/assessments/evaluate",
        json=assessment_payload(),
        headers=headers,
    )
    assert response.status_code == 403
    assert "assessment:write" not in response.text


def test_evaluation_endpoint_validates_body_after_authorization(tmp_path, monkeypatch):
    headers = service_headers(tmp_path, monkeypatch)
    response = client.post(
        "/api/v1/assessments/evaluate",
        json={"problem_statement": "too short", "desired_outcome": "ok"},
        headers=headers,
    )
    assert response.status_code == 422
