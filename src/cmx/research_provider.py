"""Brave Search API adapter for bounded, source-attributed web discovery.

Requires CMX_BRAVE_SEARCH_API_KEY. This adapter retrieves search-result metadata;
it does not crawl pages or verify claims. Never log the API key or raw response.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cmx.research import ResearchProviderUnavailable, ResearchQuery, ResearchSource


class BraveSearchProvider:
    """Small bounded adapter using Brave's documented web-search endpoint."""

    name = "brave_search"

    def __init__(self, api_key: str, *, timeout_seconds: float = 8.0) -> None:
        if not api_key or not api_key.strip():
            raise ResearchProviderUnavailable("Brave Search API key is not configured.")
        if not 1 <= timeout_seconds <= 15:
            raise ValueError("Search timeout must be between 1 and 15 seconds.")
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "BraveSearchProvider":
        api_key = os.environ.get("CMX_BRAVE_SEARCH_API_KEY", "")
        return cls(api_key)

    def search(self, request: ResearchQuery) -> list[ResearchSource]:
        query = request.query
        if request.region.casefold() != "global":
            query = f"{query} {request.region}"
        url = "https://api.search.brave.com/res/v1/web/search"
        params = f"q={_url_quote(query)}&count={request.max_results}&safesearch=moderate"
        req = Request(
            f"{url}?{params}",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self._api_key,
                "User-Agent": "CMX-Research/0.1",
            },
            method="GET",
        )
        try:
            with urlopen(req, timeout=self._timeout_seconds) as response:
                if response.status != 200:
                    raise ResearchProviderUnavailable("Search provider returned an unsuccessful response.")
                raw = response.read(1_000_001)
        except HTTPError as exc:
            # Do not expose upstream response bodies, headers, or credential details.
            if exc.code in {401, 403}:
                raise ResearchProviderUnavailable("Search provider authentication failed; check server configuration.") from exc
            if exc.code == 429:
                raise ResearchProviderUnavailable("Search provider rate limit reached; retry later.") from exc
            raise ResearchProviderUnavailable("Search provider request failed.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ResearchProviderUnavailable("Search provider is temporarily unreachable.") from exc
        if len(raw) > 1_000_000:
            raise ResearchProviderUnavailable("Search provider response exceeded the allowed size.")
        try:
            payload = json.loads(raw)
            rows = payload.get("web", {}).get("results", [])
            if not isinstance(rows, list):
                raise ValueError("unexpected results shape")
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            raise ResearchProviderUnavailable("Search provider returned an invalid response.") from exc

        retrieved_at = datetime.now(timezone.utc)
        results: list[ResearchSource] = []
        for row in rows[: request.max_results]:
            if not isinstance(row, dict):
                continue
            title = row.get("title")
            target = row.get("url")
            description = row.get("description") or ""
            if not isinstance(title, str) or not title.strip() or not isinstance(target, str):
                continue
            try:
                results.append(
                    ResearchSource(
                        title=title.strip()[:300],
                        url=target,
                        snippet=description[:4000] if isinstance(description, str) else "",
                        provider=self.name,
                        retrieved_at=retrieved_at,
                    )
                )
            except Exception:
                # Discard malformed/untrusted result rows, not the entire search.
                continue
        return results


def _url_quote(value: str) -> str:
    """Encode a query parameter without allowing URL delimiters to escape it."""
    from urllib.parse import quote

    return quote(value, safe="")
