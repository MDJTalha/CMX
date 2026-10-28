"""Provider-neutral contracts for source-backed market research.

This module deliberately supplies contracts and normalization only. It does not
fetch the web, verify vendor claims, or infer prices. A configured provider must
return source-backed results and retrieval timestamps.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class ResearchProviderUnavailable(RuntimeError):
    """Raised when no real retrieval provider has been configured."""


class EvidenceState(StrEnum):
    RETRIEVED_UNVERIFIED = "retrieved_unverified"
    CORROBORATED = "corroborated"
    CONTRADICTED = "contradicted"
    STALE = "stale"


class ResearchQuery(BaseModel):
    """Bounded user intent for a vendor-neutral discovery request."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=3, max_length=500)
    region: str = Field(default="global", min_length=2, max_length=80)
    max_results: int = Field(default=5, ge=1, le=10)


class ResearchSource(BaseModel):
    """A retrievable source record; its contents are not automatically trusted."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    url: HttpUrl
    snippet: str = Field(default="", max_length=4000)
    provider: str = Field(min_length=1, max_length=80)
    retrieved_at: datetime
    published_at: datetime | None = None

    @field_validator("url")
    @classmethod
    def require_https_source(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError("Research source URLs must use HTTPS.")
        host = (value.host or "").casefold()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            raise ValueError("Localhost is not an acceptable research source.")
        return value

    @field_validator("retrieved_at", "published_at")
    @classmethod
    def require_timezone_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Research timestamps must include a timezone.")
        return value


class EvidenceClaim(BaseModel):
    """A bounded claim tied to a specific source and retrieval event."""

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=3, max_length=1000)
    source_url: HttpUrl
    supporting_excerpt: str = Field(min_length=1, max_length=2000)
    state: EvidenceState = EvidenceState.RETRIEVED_UNVERIFIED
    retrieved_at: datetime
    reviewer_note: str = Field(default="", max_length=1000)

    @field_validator("source_url")
    @classmethod
    def require_https_claim_source(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError("Evidence claim URLs must use HTTPS.")
        return value

    @field_validator("retrieved_at")
    @classmethod
    def require_timezone_aware_retrieval(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Evidence timestamps must include a timezone.")
        return value


class ResearchProvider(Protocol):
    """Adapter contract for a real search/retrieval service."""

    name: str

    def search(self, request: ResearchQuery) -> list[ResearchSource]:
        """Return only sources actually retrieved by this provider."""
        ...


class ResearchResult(BaseModel):
    """Honest response envelope that distinguishes empty results from failure."""

    query: ResearchQuery
    provider: str
    retrieved_at: datetime
    sources: list[ResearchSource] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def normalize_source_url(url: str) -> str:
    """Normalize safe HTTPS URLs for deduplication without changing their meaning."""
    parts = urlsplit(url)
    if parts.scheme.casefold() != "https" or not parts.hostname:
        raise ValueError("Only absolute HTTPS source URLs are accepted.")
    if parts.username or parts.password:
        raise ValueError("Source URLs must not contain embedded credentials.")
    host = parts.hostname.casefold()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Localhost is not an acceptable research source.")
    port = parts.port
    netloc = host if port in (None, 443) else f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit(("https", netloc, path, parts.query, ""))


def retrieve_sources(request: ResearchQuery, provider: ResearchProvider | None) -> ResearchResult:
    """Retrieve real sources through an explicitly injected provider.

    There is intentionally no mock/default provider: without one, callers must
    surface an unavailable state instead of presenting fabricated vendor results.
    """
    if provider is None:
        raise ResearchProviderUnavailable(
            "No market-research provider is configured; no sources were retrieved."
        )
    retrieved_at = datetime.now(timezone.utc)
    raw_sources = provider.search(request)
    unique: list[ResearchSource] = []
    seen: set[str] = set()
    for source in raw_sources:
        normalized = normalize_source_url(str(source.url))
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(source)
        if len(unique) >= request.max_results:
            break
    return ResearchResult(
        query=request,
        provider=provider.name,
        retrieved_at=retrieved_at,
        sources=unique,
        limitations=[
            "Retrieved pages are unverified evidence, not proof of vendor claims.",
            "Pricing and regional availability must be supported by their own dated sources.",
            "Search snippets may omit context; review the source page before relying on a claim.",
        ],
    )
