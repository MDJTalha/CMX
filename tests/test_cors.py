"""CORS allowlist behavior for local browser development."""

from fastapi.testclient import TestClient

from cmx.main import app


def test_local_frontend_origin_is_allowed_for_health():
    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={"Origin": "http://127.0.0.1:4173"},
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"
    assert response.headers.get("access-control-allow-credentials") != "true"


def test_unlisted_origin_does_not_receive_cors_permission():
    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={"Origin": "https://untrusted.example"},
        )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_preflight_allows_bearer_header_from_local_frontend():
    with TestClient(app) as client:
        response = client.options(
            "/api/v1/assessments/evaluate",
            headers={
                "Origin": "http://localhost:4173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,x-cmx-tenant",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:4173"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert "x-cmx-tenant" in response.headers["access-control-allow-headers"].lower()
    assert "post" in response.headers["access-control-allow-methods"].lower()
    assert response.headers.get("access-control-allow-credentials") != "true"


def test_preflight_rejects_unlisted_origin():
    with TestClient(app) as client:
        response = client.options(
            "/api/v1/assessments/evaluate",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,x-cmx-tenant",
            },
        )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_protected_api_auth_error_is_readable_from_allowed_browser_origin():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assessments/evaluate",
            headers={"Origin": "http://127.0.0.1:4173"},
            json={},
        )
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"
    assert response.headers["www-authenticate"] == "Bearer"
