import json
from datetime import timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from cmx.research import ResearchProviderUnavailable, ResearchQuery
from cmx.research_provider import BraveSearchProvider


class FakeResponse:
    status = 200

    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.payload[:size]


def test_provider_requires_server_side_api_key(monkeypatch):
    monkeypatch.delenv("CMX_BRAVE_SEARCH_API_KEY", raising=False)
    with pytest.raises(ResearchProviderUnavailable, match="not configured"):
        BraveSearchProvider.from_environment()


def test_provider_maps_real_search_results_to_unverified_sources(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return FakeResponse(json.dumps({
            "web": {"results": [
                {"title": "Vendor pricing", "url": "https://vendor.example/pricing", "description": "Published plans"},
                {"title": "Second source", "url": "https://vendor.example/features", "description": "Feature page"},
            ]}
        }).encode())

    monkeypatch.setattr("cmx.research_provider.urlopen", fake_urlopen)
    provider = BraveSearchProvider("test-secret", timeout_seconds=3)
    results = provider.search(ResearchQuery(query="payroll software", region="Pakistan", max_results=1))

    assert len(results) == 1
    assert results[0].title == "Vendor pricing"
    assert str(results[0].url) == "https://vendor.example/pricing"
    assert results[0].provider == "brave_search"
    assert results[0].retrieved_at.tzinfo == timezone.utc
    assert results[0].retrieved_at.utcoffset().total_seconds() == 0
    req = seen["request"]
    assert isinstance(req, Request)
    assert req.get_header("X-subscription-token") == "test-secret"
    assert "Pakistan" in req.full_url
    assert seen["timeout"] == 3


@pytest.mark.parametrize("status,message", [
    (401, "authentication failed"),
    (403, "authentication failed"),
    (429, "rate limit"),
    (500, "request failed"),
])
def test_provider_handles_http_errors_without_exposing_upstream_body(monkeypatch, status, message):
    def fail(*_args, **_kwargs):
        raise HTTPError("https://api.search.brave.com", status, "error", {}, None)

    monkeypatch.setattr("cmx.research_provider.urlopen", fail)
    with pytest.raises(ResearchProviderUnavailable, match=message) as exc:
        BraveSearchProvider("top-secret-key").search(ResearchQuery(query="payroll"))
    assert "top-secret-key" not in str(exc.value)


def test_provider_handles_network_timeout(monkeypatch):
    def fail(*_args, **_kwargs):
        raise URLError("network down")

    monkeypatch.setattr("cmx.research_provider.urlopen", fail)
    with pytest.raises(ResearchProviderUnavailable, match="temporarily unreachable"):
        BraveSearchProvider("test-key").search(ResearchQuery(query="payroll"))


def test_provider_rejects_oversized_response(monkeypatch):
    monkeypatch.setattr(
        "cmx.research_provider.urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"x" * 1_000_001),
    )
    with pytest.raises(ResearchProviderUnavailable, match="exceeded the allowed size"):
        BraveSearchProvider("test-key").search(ResearchQuery(query="payroll"))


def test_provider_rejects_malformed_json(monkeypatch):
    monkeypatch.setattr(
        "cmx.research_provider.urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"not-json"),
    )
    with pytest.raises(ResearchProviderUnavailable, match="invalid response"):
        BraveSearchProvider("test-key").search(ResearchQuery(query="payroll"))


def test_provider_discards_non_https_and_malformed_rows(monkeypatch):
    payload = {"web": {"results": [
        {"title": "Unsafe", "url": "http://vendor.example/", "description": "no"},
        {"title": "Good", "url": "https://vendor.example/", "description": "yes"},
        {"title": "", "url": "https://vendor.example/empty"},
    ]}}
    monkeypatch.setattr(
        "cmx.research_provider.urlopen",
        lambda *_args, **_kwargs: FakeResponse(json.dumps(payload).encode()),
    )
    results = BraveSearchProvider("test-key").search(ResearchQuery(query="payroll"))
    assert [item.title for item in results] == ["Good"]
