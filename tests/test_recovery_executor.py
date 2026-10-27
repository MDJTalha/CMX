import hashlib

import pytest

from cmx.idempotency import SQLiteIdempotencyStore
from cmx.persistence import SQLiteTaskStore
from cmx.recovery import ProviderObservation, ReconciliationDecision
from cmx.recovery_executor import RecoveryExecutionError, SQLiteRecoveryExecutor
from cmx.retry_authorization import SQLiteRetryAuthorizationStore
from cmx.security import PrincipalContext


def principal(subject, permissions=None):
    return PrincipalContext(
        subject_id=subject,
        tenant_id="tenant_alpha",
        issuer="https://identity.cmx.test",
        permissions=frozenset(permissions or {"workflow:approve", "workflow:execute", "workflow:read"}),
        issued_at=100,
        expires_at=1000,
        authentication_verified=True,
        authentication_method="test",
        credential_id=f"cred_{subject}",
    )


class FakeProvider:
    provider_id = "provider_test"
    supports_idempotency = True

    def __init__(self, observation):
        self.observation = observation
        self.executions = []

    def lookup(self, *, operation_id, idempotency_key):
        assert self.observation.operation_id == operation_id
        assert self.observation.idempotency_key == idempotency_key
        return self.observation

    def execute_idempotently(self, *, operation_id, idempotency_key, request_payload):
        self.executions.append((operation_id, idempotency_key, request_payload))
        return {"status": "accepted", "provider_reference": "provider-ref-123"}


def setup(tmp_path):
    tasks = SQLiteTaskStore(tmp_path / "recovery-executor.sqlite3")
    ledger = SQLiteIdempotencyStore(tasks)
    authorizations = SQLiteRetryAuthorizationStore(tasks, ledger)
    approver = principal("approver_1")
    executor = principal("executor_1")
    payload = {"amount": 42, "currency": "PKR"}
    key = hashlib.sha256(b"recovery-operation").hexdigest()
    record, created = ledger.reserve(
        "task_001:payment", key, payload, principal=approver,
        occurred_at=150, authorization_now_epoch=150,
    )
    assert created
    ledger.mark_dispatching(key, principal=approver, occurred_at=151, authorization_now_epoch=151)
    uncertain = ledger.mark_uncertain(key, principal=approver, occurred_at=152, authorization_now_epoch=152)
    decision = ReconciliationDecision(
        disposition="retry_candidate",
        safe_to_replay=False,
        requires_single_use_authorization=True,
        reason="provider confirmed non-application",
        evidence_ref="provider-evidence:case-123",
    )
    issued = authorizations.issue(uncertain, decision, principal=approver, now_epoch=200)
    observation = ProviderObservation(
        provider_id="provider_test",
        operation_id=uncertain.operation_id,
        idempotency_key=key,
        outcome="not_applied",
        observed_at=200,
        evidence_ref="provider-evidence:case-123",
        provider_request_id="req-123",
    )
    provider = FakeProvider(observation)
    executor_service = SQLiteRecoveryExecutor(ledger, authorizations)
    return tasks, ledger, authorizations, approver, executor, payload, key, uncertain, issued, provider, executor_service


def test_recovery_executes_only_after_fresh_provider_evidence_and_single_use_ticket(tmp_path):
    _, ledger, _, _, executor, payload, key, _, issued, provider, service = setup(tmp_path)

    result = service.execute(
        idempotency_key=key,
        request_payload=payload,
        authorization_token=issued.token,
        provider=provider,
        principal=executor,
        now_epoch=201,
    )

    assert result.disposition == "succeeded"
    assert result.provider_id == "provider_test"
    assert result.authorization_id == issued.authorization_id
    assert result.result == {"status": "accepted", "provider_reference": "provider-ref-123"}
    assert provider.executions == [(result.operation_id, key, payload)]
    durable = ledger.get(key, principal=executor, authorization_now_epoch=202)
    assert durable.state == "succeeded"
    events = ledger.list_events(key, principal=executor, authorization_now_epoch=202)
    assert [event.event_type for event in events][-2:] == [
        "operation.recovery_dispatching", "operation.succeeded"
    ]


def test_unknown_provider_outcome_never_consumes_ticket_or_executes(tmp_path):
    _, _, auth, _, executor, payload, key, record, issued, provider, service = setup(tmp_path)
    provider.observation = provider.observation.model_copy(update={"outcome": "unknown"})

    with pytest.raises(RecoveryExecutionError, match="manual_review"):
        service.execute(
            idempotency_key=key,
            request_payload=payload,
            authorization_token=issued.token,
            provider=provider,
            principal=executor,
            now_epoch=201,
        )

    assert provider.executions == []
    # A failed preflight must not burn the ticket.
    consumed = auth.consume(
        issued.token,
        operation_id=record.operation_id,
        idempotency_key=record.idempotency_key,
        request_hash=record.request_hash,
        evidence_ref="provider-evidence:case-123",
        principal=executor,
        now_epoch=202,
    )
    assert consumed.authorization_id == issued.authorization_id


def test_payload_mismatch_fails_before_provider_lookup_or_ticket_consumption(tmp_path):
    _, _, auth, _, executor, _, key, record, issued, provider, service = setup(tmp_path)

    with pytest.raises(Exception, match="request hash"):
        service.execute(
            idempotency_key=key,
            request_payload={"amount": 999, "currency": "PKR"},
            authorization_token=issued.token,
            provider=provider,
            principal=executor,
            now_epoch=201,
        )

    assert provider.executions == []
    consumed = auth.consume(
        issued.token,
        operation_id=record.operation_id,
        idempotency_key=record.idempotency_key,
        request_hash=record.request_hash,
        evidence_ref="provider-evidence:case-123",
        principal=executor,
        now_epoch=202,
    )
    assert consumed.authorization_id == issued.authorization_id


def test_provider_failure_returns_operation_to_uncertain_and_requires_reconciliation(tmp_path):
    _, ledger, _, _, executor, payload, key, _, issued, provider, service = setup(tmp_path)

    def fail(**kwargs):
        provider.executions.append(kwargs)
        raise TimeoutError("simulated lost provider response")

    provider.execute_idempotently = fail
    with pytest.raises(RecoveryExecutionError, match="reconcile provider state"):
        service.execute(
            idempotency_key=key,
            request_payload=payload,
            authorization_token=issued.token,
            provider=provider,
            principal=executor,
            now_epoch=201,
        )

    durable = ledger.get(key, principal=executor, authorization_now_epoch=202)
    assert durable.state == "uncertain"
    assert len(provider.executions) == 1
