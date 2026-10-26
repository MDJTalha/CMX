from fastapi.testclient import TestClient

from cmx.main import app

client = TestClient(app)


def test_health_endpoint_reports_service_identity():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "cmx-decision-engine"


def test_api_metadata_identifies_cmx():
    response = client.get("/api/v1")
    assert response.status_code == 200
    assert response.json()["brand"] == "CMX"
    assert response.json()["meaning"] == "Customer & Market Experience"


def test_evaluation_endpoint_accepts_structured_request():
    payload = {
        "problem_statement": "Reduce manual enquiry processing.",
        "desired_outcome": "Reduce handling time.",
        "region": "global",
        "max_first_year_cost_usd": 1000,
        "requirements": [
            {"key": "email_integration", "label": "Email integration", "mandatory": True, "weight": 5}
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
                "source_urls": ["https://example.com/product"]
            }
        ]
    }
    response = client.post("/api/v1/assessments/evaluate", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "recommendation_ready"
    assert body["recommended_candidate_id"] == "demo_solution"


def test_evaluation_endpoint_rejects_invalid_request():
    response = client.post(
        "/api/v1/assessments/evaluate",
        json={"problem_statement": "too short", "desired_outcome": "ok"},
    )
    assert response.status_code == 422
