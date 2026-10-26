"""Provider-neutral model adapter registry and safe deterministic fallback."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from cmx.evidence import (
    ModelGateway, ModelProvider, ModelRequest, ModelResponse, ProviderPolicy,
    ProviderError, ProviderUnavailable,
)


class DeterministicUnavailableProvider:
    """Explicit no-network adapter used when no hosted/local provider is configured."""

    provider_name = "disabled"

    def generate(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        raise ProviderUnavailable(
            "No model provider is configured. Use the deterministic decision kernel or configure an adapter."
        )


class ProviderRegistry:
    """Resolve only explicitly configured providers; never silently switch vendors."""

    def __init__(self, providers: Sequence[ModelProvider] = (), policy: ProviderPolicy | None = None) -> None:
        configured = list(providers)
        names = [provider.provider_name for provider in configured]
        if len(names) != len(set(names)):
            raise ValueError("Provider names must be unique.")
        self._gateway = ModelGateway(configured, policy)

    @property
    def configured_providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._gateway._providers))

    def generate(self, provider_name: str, request: ModelRequest) -> ModelResponse:
        return self._gateway.generate(provider_name, request)


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    configured: bool
    detail: str


def provider_status(registry: ProviderRegistry) -> tuple[ProviderStatus, ...]:
    names = registry.configured_providers
    if not names:
        return (ProviderStatus("none", False, "No LLM provider configured; deterministic kernel remains available."),)
    return tuple(ProviderStatus(name, True, "Configured; credentials/connectivity have not been independently health-checked.") for name in names)
