"""Fail-closed preflight for a separately authorized recovery attempt.

This module does not dispatch external effects. It validates the current durable record,
original request payload, fresh provider evidence, and single-use authorization before
returning an auditable preflight result for a future executor implementation.
"""
from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict

from cmx.idempotency import SQLiteIdempotencyStore
from cmx.persistence import _canonical_json
from cmx.recovery import ProviderObservation, ReconciliationDecision, evaluate_provider_observation
from cmx.retry_authorization import ConsumedRetryAuthorization, SQLiteRetryAuthorizationStore
from cmx.security import PrincipalContext


class RecoveryPreflightError(RuntimeError):
    """Recovery preflight failed; no external effect was dispatched."""


class RecoveryPreflightResult(BaseModel):
    """Validated, one-time authorization metadata; not a dispatch guarantee."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    tenant_id: str
    idempotency_key: str
    request_hash: str
    provider_id: str
    evidence_ref: str
    decision: ReconciliationDecision
    authorization: ConsumedRetryAuthorization


class RecoveryExecutionPreflight:
    """Validate recovery inputs and consume authorization without invoking a provider."""

    def __init__(
        self,
        idempotency_store: SQLiteIdempotencyStore,
        authorization_store: SQLiteRetryAuthorizationStore,
    ) -> None:
        self.idempotency_store = idempotency_store
        self.authorization_store = authorization_store

    def prepare(
        self,
        idempotency_key: str,
        *,
        request_payload: Any,
        observation: ProviderObservation,
        expected_provider_id: str,
        provider_idempotency_supported: bool,
        authorization_token: str,
        principal: PrincipalContext,
        now_epoch: int,
        max_evidence_age_seconds: int = 300,
    ) -> RecoveryPreflightResult:
        record = self.idempotency_store.get(
            idempotency_key, principal=principal, authorization_now_epoch=now_epoch
        )
        if record.state not in {"dispatching", "uncertain"}:
            raise RecoveryPreflightError("Operation is not eligible for recovery preflight.")
        actual_hash = hashlib.sha256(_canonical_json(request_payload).encode("utf-8")).hexdigest()
        if actual_hash != record.request_hash:
            raise RecoveryPreflightError("Original request payload does not match the durable request hash.")
        decision = evaluate_provider_observation(
            record,
            observation,
            expected_provider_id=expected_provider_id,
            now_epoch=now_epoch,
            max_evidence_age_seconds=max_evidence_age_seconds,
            provider_idempotency_supported=provider_idempotency_supported,
        )
        if decision.disposition != "retry_candidate" or decision.evidence_ref != observation.evidence_ref:
            raise RecoveryPreflightError(
                f"Provider evidence is not eligible for retry preflight: {decision.disposition}."
            )
        if observation.provider_id != expected_provider_id:
            raise RecoveryPreflightError("Provider identity does not match the configured adapter.")
        try:
            consumed, claimed_record = self.authorization_store.consume_and_claim_recovery(
                authorization_token,
                operation_id=record.operation_id,
                idempotency_key=record.idempotency_key,
                request_hash=record.request_hash,
                evidence_ref=observation.evidence_ref,
                principal=principal,
                now_epoch=now_epoch,
            )
        except Exception as exc:
            from cmx.retry_authorization import RetryAuthorizationError
            if isinstance(exc, RetryAuthorizationError):
                raise RecoveryPreflightError(str(exc)) from exc
            raise
        return RecoveryPreflightResult(
            operation_id=claimed_record.operation_id,
            tenant_id=claimed_record.tenant_id,
            idempotency_key=claimed_record.idempotency_key,
            request_hash=claimed_record.request_hash,
            provider_id=expected_provider_id,
            evidence_ref=observation.evidence_ref,
            decision=decision,
            authorization=consumed,
        )
