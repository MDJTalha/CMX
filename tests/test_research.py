from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from cmx.research import (
    EvidenceState,
    ResearchProviderUnavailable,
    ResearchQuery,
    ResearchSource,
    normalize_source_url,
    retrieve_sources,
)


def source(url: str, title: str = "Official product page") -> ResearchSource:
    return ResearchSource(
        title=title,
        url=url,
        snippet="The page describes the product.",
        provider="test-provider",
        retrieved_at=datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc),
    )


def test_research_query_is_bounded_and_rejects_unknown_fields():
    assert ResearchQuery(query="payroll software", max_results=3).max_results == 3
    with pytest.raises(ValidationError):
        ResearchQuery(query="ok", max_results=100)
    with pytest.raises(ValidationError):
        ResearchQuery(query="payroll", arbitrary=True)


def test_source_requires_https_and_timezone_aware_retrieval_timestamp():
    with pytest.raises(ValidationError):
        source("http://vendor.example/pricing")
    with pytest.raises(ValidationError):
        ResearchSource(
            title="Pricing",
            url="https://vendor.example/pricing",
            provider="test-provider",
            retrieved_at=datetime(2026, 10, 28, 10, 0),
        )


def test_url_normalization_removes_fragment_and_default_port():
    assert normalize_source_url("https://Vendor.Example:443/pricing#plans") == (
        "https://vendor.example/pricing"
    )


def test_url_normalization_rejects_non_https_and_embedded_credentials():
    with pytest.raises(ValueError):
        normalize_source_url("http://vendor.example/")
    with pytest.raises(ValueError):
        normalize_source_url("https://user:secret@vendor.example/")


def test_missing_provider_fails_closed_instead_of_fabricating_results():
    with pytest.raises(ResearchProviderUnavailable, match="no sources were retrieved"):
        retrieve_sources(ResearchQuery(query="payroll software"), None)


def test_retrieval_deduplicates_urls_and_limits_results():
    class FakeProvider:
        name = "controlled-test-provider"

        def search(self, request):
            return [
                source("https://vendor.example/product#overview", "Product"),
                source("https://vendor.example/product", "Duplicate"),
                source("https://vendor.example/pricing", "Pricing"),
            ]

    result = retrieve_sources(
        ResearchQuery(query="payroll software", max_results=2),
        FakeProvider(),
    )
    assert result.provider == "controlled-test-provider"
    assert len(result.sources) == 2
    assert result.sources[0].title == "Product"
    assert result.sources[1].title == "Pricing"
    assert result.sources[0].url == "https://vendor.example/product#overview"
    assert result.sources[0].retrieved_at.tzinfo is not None
    assert "unverified evidence" in result.limitations[0]


def test_claim_states_do_not_default_to_verified():
    from cmx.research import EvidenceClaim

    claim = EvidenceClaim(
        claim="Vendor supports payroll exports",
        source_url="https://vendor.example/features",
        supporting_excerpt="Exports are available.",
        retrieved_at=datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc),
    )
    assert claim.state == EvidenceState.RETRIEVED_UNVERIFIED
