"""Crash-aware, provider-aware recovery execution for uncertain operations.

This executor is deliberately narrow: it only resumes a durable operation after a fresh,
correctly bound provider observation reports non-application, a single-use authorization
ticket is consumed by a distinct executor, and the provider advertises idempotency support.
It does not promise exactly-once external effects. Provider adapters must authenticate lookup
responses and honor the supplied idempotency key on execution.
"""
from __future__ import annotations

import hashlib
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from cmx.idempotency import (
    IdempotencyConflict,
    IdempotencyRecord,
    SQLiteIdempotencyStore,
)
from cmx.persistence import _canonical_json, _timestamp
from cmx.recovery import (
    ProviderObservation,
    ProviderReconciliationAdapter,
    evaluate_provider_observation,
)
from cmx.retry_authorization import (
    ConsumedRetryAuthorization,
    SQLiteRetryAuthorizationStore,
)
from cmx.security import PrincipalContext


class RecoveryExecutionError(RuntimeError):
    """Recovery did not reach a confirmed terminal result; reconcile before any further action."""


class IdempotentRecoveryProvider(ProviderReconciliationAdapter, Protocol):
    """Provider adapter that can look up and safely repeat an idempotent operation."""

    supports_idempotency: bool

    def execute_idempotently(
        self, *, operation_id: str, idempotency_key: str, request_payload: Any
    ) -> Any:
        """Perform the operation using the exact stable idempotency key."""
        ...


class RecoveryExecutionResult(BaseModel):
    """Auditable outcome summary; contains no bearer authorization token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    idempotency_key: str
    provider_id: str
    disposition: str
    authorization_id: str
    result: Any = None


def _request_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class SQLiteRecoveryExecutor:
    """Execute a reviewed recovery without silently replaying unknown external effects."""

    def __init__(
        self,
        idempotency_store: SQLiteIdempotencyStore,
        authorization_store: SQLiteRetryAuthorizationStore,
    ) -> None:
        if idempotency_store.task_store.database_path != authorization_store.task_store.database_path:
            raise ValueError("Idempotency and recovery authorization stores must share one database.")
        if authorization_store.idempotency_store is not idempotency_store:
            raise ValueError("Authorization store must be bound to the same idempotency store.")
        self.idempotency_store = idempotency_store
        self.authorization_store = authorization_store
        self.task_store = idempotency_store.task_store

    def execute(
        self,
        *,
        idempotency_key: str,
        request_payload: Any,
        authorization_token: str,
        provider: IdempotentRecoveryProvider,
        principal: PrincipalContext,
        now_epoch: int | None = None,
        max_evidence_age_seconds: int = 300,
    ) -> RecoveryExecutionResult:
        """Reconcile, consume authorization, durably mark dispatch, then call the provider.

        If the process crashes after dispatch is recorded, the operation remains
        dispatching and must be reconciled again. If the provider call raises, the record is
        marked uncertain where possible. The executor never falls back to a blind replay.
        """
        now = _timestamp(now_epoch)
        record = self.idempotency_store.get(
            idempotency_key, principal=principal, authorization_now_epoch=now
        )
        if record.state != "uncertain":
            raise RecoveryExecutionError("Only an uncertain operation can enter this recovery path.")
        if _request_hash(request_payload) != record.request_hash:
            raise IdempotencyConflict("Recovery payload does not match the durable request hash.")
        if not isinstance(provider.provider_id, str) or not provider.provider_id:
            raise RecoveryExecutionError("Provider adapter identity is missing.")
        if provider.supports_idempotency is not True:
            raise RecoveryExecutionError("Provider does not explicitly support idempotent execution.")

        observation = provider.lookup(
            operation_id=record.operation_id, idempotency_key=record.idempotency_key
        )
        if not isinstance(observation, ProviderObservation):
            raise RecoveryExecutionError("Provider lookup did not return a validated observation.")
        decision = evaluate_provider_observation(
            record,
            observation,
            expected_provider_id=provider.provider_id,
            now_epoch=now,
            max_evidence_age_seconds=max_evidence_age_seconds,
            provider_idempotency_supported=True,
        )
        if decision.disposition != "retry_candidate" or not decision.evidence_ref:
            raise RecoveryExecutionError(
                f"Provider evidence does not authorize recovery execution: {decision.disposition}."
            )

        # Consumption is intentionally durable before any side effect. A crash after this
        # point burns the ticket; operators must reconcile and issue a fresh ticket.
        consumed: ConsumedRetryAuthorization = self.authorization_store.consume(
            authorization_token,
            operation_id=record.operation_id,
            idempotency_key=record.idempotency_key,
            request_hash=record.request_hash,
            evidence_ref=decision.evidence_ref,
            principal=principal,
            now_epoch=now,
        )

        # Atomically fence the local transition against stale state. If this fails, the
        # ticket stays consumed and no provider call occurs; this is fail-safe.
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """SELECT * FROM cmx_idempotency_operations
                       WHERE tenant_id = ? AND idempotency_key = ?""",
                    (principal.tenant_id, record.idempotency_key),
                ).fetchone()
                if row is None or row["operation_id"] != record.operation_id:
                    raise RecoveryExecutionError("Durable operation binding changed before dispatch.")
                if row["request_hash"] != record.request_hash or row["state"] != "uncertain":
                    raise RecoveryExecutionError("Durable operation state changed before recovery dispatch.")
                connection.execute(
                    """UPDATE cmx_idempotency_operations
                       SET state = 'dispatching', updated_at = ?
                       WHERE tenant_id = ? AND idempotency_key = ? AND state = 'uncertain'""",
                    (now, principal.tenant_id, record.idempotency_key),
                )
                if connection.total_changes < 1:
                    raise RecoveryExecutionError("Could not claim the uncertain operation for recovery.")
                self.idempotency_store._append_event(
                    connection,
                    tenant_id=principal.tenant_id,
                    key=record.idempotency_key,
                    actor_id=principal.subject_id,
                    event_type="operation.recovery_dispatching",
                    occurred_at=now,
                    payload={
                        "operation_id": record.operation_id,
                        "from_state": "uncertain",
                        "to_state": "dispatching",
                        "authorization_id": consumed.authorization_id,
                        "evidence_ref": decision.evidence_ref,
                        "provider_id": provider.provider_id,
                        "provider_idempotency": True,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

        try:
            result = provider.execute_idempotently(
                operation_id=record.operation_id,
                idempotency_key=record.idempotency_key,
                request_payload=request_payload,
            )
            # Validate serializability before the durable terminal transition. If this
            # fails, preserve uncertainty rather than inventing a successful result.
            _canonical_json(result)
            succeeded = self.idempotency_store.mark_succeeded(
                record.idempotency_key,
                result,
                principal=principal,
                occurred_at=now,
                authorization_now_epoch=now,
            )
            return RecoveryExecutionResult(
                operation_id=succeeded.operation_id,
                idempotency_key=succeeded.idempotency_key,
                provider_id=provider.provider_id,
                disposition="succeeded",
                authorization_id=consumed.authorization_id,
                result=succeeded.result,
            )
        except Exception as exc:
            try:
                current = self.idempotency_store.get(
                    record.idempotency_key, principal=principal, authorization_now_epoch=now
                )
                if current.state == "dispatching":
                    self.idempotency_store.mark_uncertain(
                        record.idempotency_key,
                        principal=principal,
                        occurred_at=now,
                        authorization_now_epoch=now,
                    )
            except Exception:
                # Preserve the original error. A dispatching record is itself a durable
                # signal that the external outcome must be reconciled before another attempt.
                pass
            raise RecoveryExecutionError(
                "Recovery execution did not confirm success; reconcile provider state before any further attempt."
            ) from exc
