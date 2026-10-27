import time

from fastapi.testclient import TestClient

from cmx.main import app
from cmx.service_auth import SQLiteCredentialStore

client = TestClient(app)


def service_headers(tmp_path, monkeypatch, permissions=("assessment:write",)):
    database_path = tmp_path / "cmx-auth.sqlite3"
    monkeypatch.setenv("CMX_AUTH_DB_PATH", str(database_path))
    monkeypatch.setenv("CMX_ASSESSMENT_DB_PATH", str(tmp_path / "cmx-assessments.sqlite3"))
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



def test_evaluation_persists_assessment_and_returns_immutable_id(tmp_path, monkeypatch):
    headers = service_headers(tmp_path, monkeypatch)
    response = client.post("/api/v1/assessments/evaluate", json=assessment_payload(), headers=headers)
    assert response.status_code == 200
    assessment_id = response.headers.get("x-cmx-assessment-id")
    assert assessment_id and assessment_id.startswith("asm_")
    assert "assessment_id" not in response.json()


def test_assessment_history_requires_read_permission_and_is_tenant_scoped(tmp_path, monkeypatch):
    headers = service_headers(
        tmp_path, monkeypatch, permissions=("assessment:write", "assessment:read")
    )
    created = client.post("/api/v1/assessments/evaluate", json=assessment_payload(), headers=headers)
    assessment_id = created.headers["x-cmx-assessment-id"]
    listed = client.get("/api/v1/assessments", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["items"][0]["assessment_id"] == assessment_id
    fetched = client.get(f"/api/v1/assessments/{assessment_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["request"]["problem_statement"] == assessment_payload()["problem_statement"]

    other_headers = service_headers(
        tmp_path, monkeypatch, permissions=("assessment:read",)
    )
    # A newly issued credential is still scoped to tenant_test, so the resource remains visible.
    assert client.get(f"/api/v1/assessments/{assessment_id}", headers=other_headers).status_code == 200


def test_assessment_history_denies_missing_read_permission(tmp_path, monkeypatch):
    headers = service_headers(tmp_path, monkeypatch, permissions=("assessment:write",))
    response = client.get("/api/v1/assessments", headers=headers)
    assert response.status_code == 403


def test_assessment_not_found_does_not_leak_cross_tenant_records(tmp_path, monkeypatch):
    headers = service_headers(
        tmp_path, monkeypatch, permissions=("assessment:write", "assessment:read")
    )
    created = client.post("/api/v1/assessments/evaluate", json=assessment_payload(), headers=headers)
    assessment_id = created.headers["x-cmx-assessment-id"]
    # A valid credential for a different tenant must not discover the record.
    db = tmp_path / "cmx-auth.sqlite3"
    from cmx.service_auth import SQLiteCredentialStore
    store = SQLiteCredentialStore(db)
    issued = store.issue(
        subject_id="svc_other", tenant_id="tenant_other",
        permissions=["assessment:read"], expires_in_seconds=3600, now_epoch=int(time.time()),
    )
    other = {"Authorization": f"Bearer {issued.token}"}
    assert client.get(f"/api/v1/assessments/{assessment_id}", headers=other).status_code == 404
    assert client.get("/api/v1/assessments", headers=other).json()["count"] == 0
