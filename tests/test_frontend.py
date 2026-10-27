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


def test_health_and_api_metadata_remain_available_with_frontend():
    client = TestClient(app)
    health = client.get("/health")
    metadata = client.get("/api/v1")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert metadata.status_code == 200
    assert metadata.json()["brand"] == "CMX"
