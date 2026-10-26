"""Provider-neutral reconciliation contracts for interrupted external operations.

This module only evaluates provider evidence. It never invokes a provider, mutates the
ledger, marks a workflow successful, or authorizes a retry. Provider adapters are trusted
integration code and must validate provider-specific response authenticity before returning
an observation.
"""
from __future__ import annotations

import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from cmx.idempotency import IdempotencyRecord

_PROVIDER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,127}$")


class ProviderObservation(BaseModel):
    """A provider adapter's normalized, auditable lookup result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=2, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,127}$")
    operation_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["applied", "not_applied", "unknown"]
    observed_at: int = Field(ge=0)
    evidence_ref: str = Field(min_length=1, max_length=512)
    provider_request_id: str | None = Field(default=None, max_length=256)


class ProviderReconciliationAdapter(Protocol):
    """Adapter contract. Implementations must authenticate provider lookup responses."""

    provider_id: str

    def lookup(self, *, operation_id: str, idempotency_key: str) -> ProviderObservation:
        """Look up an operation without creating, replaying, or mutating it."""
        ...


class ReconciliationDecision(BaseModel):
    """Policy result; intentionally not an execution authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: Literal[
        "evidence_rejected",
        "manual_review",
        "provider_applied",
        "retry_candidate",
    ]
    safe_to_replay: Literal[False] = False
    requires_single_use_authorization: Literal[True] = True
    reason: str
    evidence_ref: str | None = None


def evaluate_provider_observation(
    record: IdempotencyRecord,
    observation: ProviderObservation,
    *,
    expected_provider_id: str,
    now_epoch: int,
    max_evidence_age_seconds: int = 300,
    provider_idempotency_supported: bool = False,
) -> ReconciliationDecision:
    """Evaluate provider evidence conservatively without mutating state or authorizing execution.

    Only a fresh, correctly bound not_applied observation from an explicitly
    idempotency-capable provider can be labelled a retry candidate. Even then, the caller
    must separately obtain single-use authorization and pass all normal execution gates.
    """
    if not isinstance(now_epoch, int) or isinstance(now_epoch, bool) or now_epoch < 0:
        raise ValueError("now_epoch must be a non-negative integer.")
    if (
        not isinstance(max_evidence_age_seconds, int)
        or isinstance(max_evidence_age_seconds, bool)
        or not 1 <= max_evidence_age_seconds <= 86400
    ):
        raise ValueError("max_evidence_age_seconds must be between 1 and 86400.")
    if not isinstance(expected_provider_id, str) or not _PROVIDER_RE.fullmatch(expected_provider_id):
        raise ValueError("expected_provider_id is invalid.")

    if (
        observation.provider_id != expected_provider_id
        or observation.operation_id != record.operation_id
        or observation.idempotency_key != record.idempotency_key
    ):
        return ReconciliationDecision(
            disposition="evidence_rejected",
            reason="Provider, operation, or idempotency-key binding does not match the durable record.",
        )

    if record.state not in {"dispatching", "uncertain"}:
        return ReconciliationDecision(
            disposition="manual_review",
            reason="Only dispatching or uncertain operations are eligible for provider reconciliation.",
            evidence_ref=observation.evidence_ref,
        )

    age = now_epoch - observation.observed_at
    if age < 0 or age > max_evidence_age_seconds:
        return ReconciliationDecision(
            disposition="manual_review",
            reason="Provider evidence is from the future or outside the configured freshness window.",
            evidence_ref=observation.evidence_ref,
        )

    if observation.outcome == "unknown":
        return ReconciliationDecision(
            disposition="manual_review",
            reason="Provider outcome is unknown; automatic replay is prohibited.",
            evidence_ref=observation.evidence_ref,
        )
    if observation.outcome == "applied":
        return ReconciliationDecision(
            disposition="provider_applied",
            reason="Provider reports the operation was applied; validate the authoritative result before resolving workflow state.",
            evidence_ref=observation.evidence_ref,
        )
    if not provider_idempotency_supported:
        return ReconciliationDecision(
            disposition="manual_review",
            reason="Non-application was observed, but the provider idempotency contract is not enabled; replay is not authorized.",
            evidence_ref=observation.evidence_ref,
        )
    return ReconciliationDecision(
        disposition="retry_candidate",
        reason="Fresh provider evidence reports non-application and the provider idempotency contract is enabled; separate single-use authorization and execution preflight are still required.",
        evidence_ref=observation.evidence_ref,
    )
