import hashlib

import pytest
from pydantic import ValidationError

from cmx.idempotency import IdempotencyRecord, SQLiteIdempotencyStore
from cmx.persistence import SQLiteTaskStore
from cmx.recovery import ProviderObservation, RecoveryReviewService, evaluate_provider_observation
from cmx.security import PrincipalContext


def make_record(*, state="uncertain"):
    key = hashlib.sha256(b"provider-recovery-key").hexdigest()
    return IdempotencyRecord(
        operation_id="operation-123",
        tenant_id="tenant-1",
        idempotency_key=key,
        operation_scope="task-1:step-1",
        request_hash=hashlib.sha256(b"payload").hexdigest(),
        state=state,
        created_at=100,
        updated_at=120,
        result=None,
    )


def make_observation(record, *, outcome="not_applied", provider_id="provider.test",
                     operation_id=None, idempotency_key=None, observed_at=200):
    return ProviderObservation(
        provider_id=provider_id,
        operation_id=operation_id or record.operation_id,
        idempotency_key=idempotency_key or record.idempotency_key,
        outcome=outcome,
        observed_at=observed_at,
        evidence_ref="ticket:provider-audit-987",
        provider_request_id="provider-request-987",
    )


def test_fresh_bound_non_application_is_only_a_retry_candidate():
    record = make_record()
    observation = make_observation(record)
    decision = evaluate_provider_observation(
        record, observation, expected_provider_id="provider.test",
        now_epoch=205, provider_idempotency_supported=True,
    )
    assert decision.disposition == "retry_candidate"
    assert decision.safe_to_replay is False
    assert decision.requires_single_use_authorization is True
    assert decision.evidence_ref == observation.evidence_ref


@pytest.mark.parametrize("field", ["provider_id", "operation_id", "idempotency_key"])
def test_mismatched_provider_binding_rejects_evidence(field):
    record = make_record()
    values = {
        "provider_id": "provider.other",
        "operation_id": "different-operation",
        "idempotency_key": hashlib.sha256(b"different-key").hexdigest(),
    }
    observation = make_observation(record, **{field: values[field]})
    decision = evaluate_provider_observation(
        record, observation, expected_provider_id="provider.test", now_epoch=205,
        provider_idempotency_supported=True,
    )
    assert decision.disposition == "evidence_rejected"
    assert decision.safe_to_replay is False


def test_non_application_without_provider_idempotency_requires_review():
    record = make_record()
    decision = evaluate_provider_observation(
        record, make_observation(record), expected_provider_id="provider.test",
        now_epoch=205, provider_idempotency_supported=False,
    )
    assert decision.disposition == "manual_review"
    assert decision.safe_to_replay is False


@pytest.mark.parametrize("outcome", ["unknown", "applied"])
def test_unknown_or_applied_outcome_never_authorizes_replay(outcome):
    record = make_record()
    decision = evaluate_provider_observation(
        record, make_observation(record, outcome=outcome),
        expected_provider_id="provider.test", now_epoch=205,
        provider_idempotency_supported=True,
    )
    assert decision.disposition == ("provider_applied" if outcome == "applied" else "manual_review")
    assert decision.safe_to_replay is False
    assert decision.requires_single_use_authorization is True


@pytest.mark.parametrize("observed_at,now_epoch", [(500, 499), (100, 500)])
def test_future_or_stale_evidence_requires_manual_review(observed_at, now_epoch):
    record = make_record()
    decision = evaluate_provider_observation(
        record, make_observation(record, observed_at=observed_at),
        expected_provider_id="provider.test", now_epoch=now_epoch,
        max_evidence_age_seconds=300, provider_idempotency_supported=True,
    )
    assert decision.disposition == "manual_review"
    assert decision.safe_to_replay is False


@pytest.mark.parametrize("state", ["reserved", "succeeded"])
def test_non_dispatching_states_are_not_reconciled_as_retry_candidates(state):
    record = make_record(state=state)
    decision = evaluate_provider_observation(
        record, make_observation(record), expected_provider_id="provider.test",
        now_epoch=205, provider_idempotency_supported=True,
    )
    assert decision.disposition == "manual_review"
    assert decision.safe_to_replay is False


@pytest.mark.parametrize("kwargs", [
    {"now_epoch": -1},
    {"now_epoch": True},
    {"max_evidence_age_seconds": 0},
    {"max_evidence_age_seconds": 86401},
    {"max_evidence_age_seconds": True},
    {"expected_provider_id": ""},
])
def test_invalid_policy_arguments_fail_closed(kwargs):
    record = make_record()
    base = {
        "expected_provider_id": "provider.test",
        "now_epoch": 205,
        "max_evidence_age_seconds": 300,
        "provider_idempotency_supported": True,
    }
    base.update(kwargs)
    with pytest.raises(ValueError):
        evaluate_provider_observation(record, make_observation(record), **base)


def test_provider_observation_rejects_malformed_keys_and_extra_fields():
    record = make_record()
    with pytest.raises(ValidationError):
        ProviderObservation(
            provider_id="provider.test", operation_id=record.operation_id,
            idempotency_key="not-a-digest", outcome="not_applied", observed_at=200,
            evidence_ref="evidence:1",
        )
    with pytest.raises(ValidationError):
        ProviderObservation(
            provider_id="provider.test", operation_id=record.operation_id,
            idempotency_key=record.idempotency_key, outcome="not_applied",
            observed_at=200, evidence_ref="evidence:1", untrusted="extra",
        )

def test_recovery_review_service_reads_ledger_and_calls_provider_lookup_only(tmp_path):
    tasks = SQLiteTaskStore(tmp_path / "recovery-review.sqlite3")
    ledger = SQLiteIdempotencyStore(tasks)
    principal = PrincipalContext(
        subject_id="operator_1", tenant_id="tenant-1", issuer="https://identity.cmx.test",
        permissions=frozenset({"workflow:execute", "workflow:read", "audit:read"}),
        issued_at=100, expires_at=1000, authentication_verified=True,
        authentication_method="test", credential_id="cred_operator_1",
    )
    key = hashlib.sha256(b"provider-recovery-key").hexdigest()
    record, created = ledger.reserve(
        "task-1:step-1", key, {"amount": 5}, principal=principal,
        occurred_at=110, authorization_now_epoch=110,
    )
    assert created
    ledger.mark_dispatching(
        key, principal=principal, occurred_at=120, authorization_now_epoch=120,
    )

    class ReadOnlyProvider:
        provider_id = "provider.test"
        def __init__(self):
            self.calls = []
        def lookup(self, *, operation_id, idempotency_key):
            self.calls.append((operation_id, idempotency_key))
            return ProviderObservation(
                provider_id=self.provider_id, operation_id=operation_id,
                idempotency_key=idempotency_key, outcome="not_applied",
                observed_at=195, evidence_ref="provider-audit:review-1",
            )

    provider = ReadOnlyProvider()
    result = RecoveryReviewService(ledger).review(
        key, adapter=provider, principal=principal, now_epoch=200,
        provider_idempotency_supported=True,
    )

    assert result.operation_id == record.operation_id
    assert result.idempotency_key == key
    assert result.request_hash == record.request_hash
    assert result.decision.disposition == "retry_candidate"
    assert result.decision.safe_to_replay is False
    assert provider.calls == [(record.operation_id, key)]
    # Reviewing evidence is read-only: it cannot advance the durable operation.
    assert ledger.get(key, principal=principal, authorization_now_epoch=200).state == "dispatching"


def test_recovery_review_service_does_not_call_provider_for_reserved_operation(tmp_path):
    tasks = SQLiteTaskStore(tmp_path / "recovery-review-reserved.sqlite3")
    ledger = SQLiteIdempotencyStore(tasks)
    principal = PrincipalContext(
        subject_id="operator_1", tenant_id="tenant-1", issuer="https://identity.cmx.test",
        permissions=frozenset({"workflow:execute", "workflow:read", "audit:read"}),
        issued_at=100, expires_at=1000, authentication_verified=True,
        authentication_method="test", credential_id="cred_operator_1",
    )
    key = hashlib.sha256(b"reserved-operation").hexdigest()
    ledger.reserve("task-1:step-1", key, {"amount": 5}, principal=principal,
                   occurred_at=110, authorization_now_epoch=110)

    class ProviderMustNotBeCalled:
        provider_id = "provider.test"
        def lookup(self, **kwargs):
            raise AssertionError("Provider lookup must not run for a reserved operation.")

    result = RecoveryReviewService(ledger).review(
        key, adapter=ProviderMustNotBeCalled(), principal=principal, now_epoch=200,
    )
    assert result.decision.disposition == "manual_review"
    assert result.decision.safe_to_replay is False
    assert result.provider_observation.outcome == "unknown"

