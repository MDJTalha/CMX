import hashlib

import pytest

from cmx.idempotency import SQLiteIdempotencyStore
from cmx.persistence import SQLiteTaskStore
from cmx.recovery import ProviderObservation, ReconciliationDecision
from cmx.recovery_execution import RecoveryExecutionPreflight, RecoveryPreflightError
from cmx.retry_authorization import RetryAuthorizationError, SQLiteRetryAuthorizationStore
from cmx.security import PrincipalContext


def principal(subject, permissions=None, tenant="tenant_alpha"):
    return PrincipalContext(
        subject_id=subject, tenant_id=tenant, issuer="https://identity.cmx.test",
        permissions=frozenset(permissions or {"workflow:approve", "workflow:execute", "workflow:read"}),
        issued_at=100, expires_at=1000, authentication_verified=True,
        authentication_method="test", credential_id=f"cred_{subject}",
    )


def setup(tmp_path):
    tasks = SQLiteTaskStore(tmp_path / "recovery-execution.sqlite3")
    ledger = SQLiteIdempotencyStore(tasks)
    authorizations = SQLiteRetryAuthorizationStore(tasks, ledger)
    approver = principal("approver")
    executor = principal("executor")
    payload = {"amount": 42, "currency": "USD"}
    key = hashlib.sha256(b"recovery-execution-key").hexdigest()
    record, created = ledger.reserve(
        "payment:step-one", key, payload, principal=approver,
        occurred_at=150, authorization_now_epoch=150,
    )
    assert created
    ledger.mark_dispatching(key, principal=approver, occurred_at=151, authorization_now_epoch=151)
    record = ledger.mark_uncertain(key, principal=approver, occurred_at=152, authorization_now_epoch=152)
    evidence_ref = "provider-evidence:case-42"
    decision = ReconciliationDecision(
        disposition="retry_candidate", safe_to_replay=False,
        requires_single_use_authorization=True,
        reason="Provider confirmed non-application; separate authorization required.",
        evidence_ref=evidence_ref,
    )
    issued = authorizations.issue(record, decision, principal=approver, now_epoch=200, ttl_seconds=120)
    observation = ProviderObservation(
        provider_id="provider.test", operation_id=record.operation_id,
        idempotency_key=key, outcome="not_applied", observed_at=195,
        evidence_ref=evidence_ref,
    )
    service = RecoveryExecutionPreflight(ledger, authorizations)
    return tasks, ledger, authorizations, approver, executor, payload, record, issued, observation, service


def test_preflight_validates_payload_and_consumes_ticket_without_dispatch(tmp_path):
    _, ledger, _, _, executor, payload, record, issued, observation, service = setup(tmp_path)
    result = service.prepare(
        record.idempotency_key, request_payload=payload, observation=observation,
        expected_provider_id="provider.test", provider_idempotency_supported=True,
        authorization_token=issued.token, principal=executor, now_epoch=201,
    )
    assert result.operation_id == record.operation_id
    assert result.request_hash == record.request_hash
    assert result.decision.disposition == "retry_candidate"
    assert result.authorization.executor_id == executor.subject_id
    assert ledger.get(record.idempotency_key, principal=executor, authorization_now_epoch=201).state == "dispatching"


def test_preflight_rejects_payload_mismatch_without_consuming_ticket(tmp_path):
    _, _, authorizations, _, executor, _, record, issued, observation, service = setup(tmp_path)
    with pytest.raises(RecoveryPreflightError, match="payload"):
        service.prepare(
            record.idempotency_key, request_payload={"amount": 999},
            observation=observation, expected_provider_id="provider.test",
            provider_idempotency_supported=True, authorization_token=issued.token,
            principal=executor, now_epoch=201,
        )
    consumed = authorizations.consume(
        issued.token, operation_id=record.operation_id,
        idempotency_key=record.idempotency_key, request_hash=record.request_hash,
        evidence_ref=observation.evidence_ref, principal=executor, now_epoch=202,
    )
    assert consumed.executor_id == executor.subject_id


@pytest.mark.parametrize("outcome", ["unknown", "applied"])
def test_preflight_rejects_unknown_or_applied_provider_outcome(tmp_path, outcome):
    _, _, authorizations, _, executor, payload, record, issued, observation, service = setup(tmp_path)
    observation = observation.model_copy(update={"outcome": outcome})
    with pytest.raises(RecoveryPreflightError, match="not eligible"):
        service.prepare(
            record.idempotency_key, request_payload=payload, observation=observation,
            expected_provider_id="provider.test", provider_idempotency_supported=True,
            authorization_token=issued.token, principal=executor, now_epoch=201,
        )
    # Failed preflight does not consume the ticket.
    consumed = authorizations.consume(
        issued.token, operation_id=record.operation_id,
        idempotency_key=record.idempotency_key, request_hash=record.request_hash,
        evidence_ref=observation.evidence_ref, principal=executor, now_epoch=202,
    )
    assert consumed.executor_id == executor.subject_id


def test_preflight_rejects_provider_idempotency_disabled(tmp_path):
    _, _, _, _, executor, payload, record, issued, observation, service = setup(tmp_path)
    with pytest.raises(RecoveryPreflightError, match="not eligible"):
        service.prepare(
            record.idempotency_key, request_payload=payload, observation=observation,
            expected_provider_id="provider.test", provider_idempotency_supported=False,
            authorization_token=issued.token, principal=executor, now_epoch=201,
        )


def test_preflight_does_not_dispatch_external_effects(tmp_path):
    _, ledger, _, _, executor, payload, record, issued, observation, service = setup(tmp_path)
    # The preflight API has no dispatch adapter parameter; it can only validate evidence
    # and consume authorization. Dispatch remains a separate, unimplemented safety boundary.
    result = service.prepare(
        record.idempotency_key, request_payload=payload, observation=observation,
        expected_provider_id="provider.test", provider_idempotency_supported=True,
        authorization_token=issued.token, principal=executor, now_epoch=201,
    )
    assert result.authorization.authorization_id == issued.authorization_id
    assert ledger.get(record.idempotency_key, principal=executor, authorization_now_epoch=201).state == "dispatching"

def test_atomic_recovery_claim_cannot_be_replayed_with_same_ticket(tmp_path):
    _, ledger, _, _, executor, payload, record, issued, observation, service = setup(tmp_path)
    first = service.prepare(
        record.idempotency_key, request_payload=payload, observation=observation,
        expected_provider_id="provider.test", provider_idempotency_supported=True,
        authorization_token=issued.token, principal=executor, now_epoch=201,
    )
    assert first.operation_id == record.operation_id
    assert ledger.get(record.idempotency_key, principal=executor, authorization_now_epoch=202).state == "dispatching"
    with pytest.raises(RecoveryPreflightError, match="consumed"):
        service.prepare(
            record.idempotency_key, request_payload=payload, observation=observation,
            expected_provider_id="provider.test", provider_idempotency_supported=True,
            authorization_token=issued.token, principal=executor, now_epoch=202,
        )


def test_atomic_recovery_claim_requires_uncertain_state(tmp_path):
    task_store, ledger, _, approver, executor, payload, record, issued, observation, service = setup(tmp_path)
    with task_store._connection() as connection:
        connection.execute(
            "UPDATE cmx_idempotency_operations SET state = 'dispatching' WHERE tenant_id = ? AND idempotency_key = ?",
            (executor.tenant_id, record.idempotency_key),
        )
    with pytest.raises(RecoveryPreflightError, match="uncertain"):
        service.prepare(
            record.idempotency_key, request_payload=payload, observation=observation,
            expected_provider_id="provider.test", provider_idempotency_supported=True,
            authorization_token=issued.token, principal=executor, now_epoch=201,
        )

