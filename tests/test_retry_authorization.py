import hashlib

import pytest

from cmx.idempotency import SQLiteIdempotencyStore
from cmx.persistence import SQLiteTaskStore
from cmx.recovery import ReconciliationDecision
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
    tasks = SQLiteTaskStore(tmp_path / "retry-auth.sqlite3")
    ledger = SQLiteIdempotencyStore(tasks)
    authorizations = SQLiteRetryAuthorizationStore(tasks, ledger)
    approver = principal("approver_1")
    executor = principal("executor_1")
    key = hashlib.sha256(b"stable-idempotency-key").hexdigest()
    record, created = ledger.reserve(
        "task_001:step_one", key, {"amount": 42}, principal=approver,
        occurred_at=150, authorization_now_epoch=150,
    )
    assert created
    ledger.mark_dispatching(key, principal=approver, occurred_at=151, authorization_now_epoch=151)
    uncertain = ledger.mark_uncertain(key, principal=approver, occurred_at=152, authorization_now_epoch=152)
    decision = ReconciliationDecision(
        disposition="retry_candidate", safe_to_replay=False,
        requires_single_use_authorization=True, reason="provider confirmed non-application",
        evidence_ref="provider-evidence:case-123",
    )
    return tasks, ledger, authorizations, approver, executor, uncertain, decision


def issue(authorizations, approver, record, decision, now=200, ttl=120):
    return authorizations.issue(record, decision, principal=approver, now_epoch=now, ttl_seconds=ttl)


def consume(authorizations, executor, record, issued, now=201, **overrides):
    args = {
        "operation_id": record.operation_id,
        "idempotency_key": record.idempotency_key,
        "request_hash": record.request_hash,
        "evidence_ref": "provider-evidence:case-123",
        "principal": executor,
        "now_epoch": now,
    }
    args.update(overrides)
    return authorizations.consume(issued.token, **args)


def test_ticket_is_single_use_and_audits_distinct_approver_executor(tmp_path):
    _, _, store, approver, executor, record, decision = setup(tmp_path)
    issued = issue(store, approver, record, decision)
    consumed = consume(store, executor, record, issued)
    assert consumed.authorization_id == issued.authorization_id
    assert consumed.approver_id == approver.subject_id
    assert consumed.executor_id == executor.subject_id
    assert consumed.consumed_at == 201
    with pytest.raises(RetryAuthorizationError, match="already been consumed"):
        consume(store, executor, record, issued, now=202)


def test_token_is_not_stored_in_plaintext(tmp_path):
    tasks, _, store, approver, _, record, decision = setup(tmp_path)
    issued = issue(store, approver, record, decision)
    with tasks._connection() as connection:
        row = connection.execute(
            "SELECT token_hash FROM cmx_retry_authorizations WHERE authorization_id=?",
            (issued.authorization_id,),
        ).fetchone()
    assert row["token_hash"] == hashlib.sha256(issued.token.encode()).hexdigest()
    assert row["token_hash"] != issued.token


@pytest.mark.parametrize("field,value", [
    ("operation_id", "other-operation"),
    ("idempotency_key", hashlib.sha256(b"other-key").hexdigest()),
    ("request_hash", hashlib.sha256(b"other-payload").hexdigest()),
    ("evidence_ref", "other-evidence"),
])
def test_binding_mismatch_does_not_consume_ticket(tmp_path, field, value):
    _, _, store, approver, executor, record, decision = setup(tmp_path)
    issued = issue(store, approver, record, decision)
    with pytest.raises(RetryAuthorizationError, match="binding does not match"):
        consume(store, executor, record, issued, **{field: value})
    assert consume(store, executor, record, issued, now=202).executor_id == executor.subject_id


def test_expired_ticket_cannot_be_consumed(tmp_path):
    _, _, store, approver, executor, record, decision = setup(tmp_path)
    issued = issue(store, approver, record, decision, now=200, ttl=5)
    with pytest.raises(RetryAuthorizationError, match="expired"):
        consume(store, executor, record, issued, now=205)


def test_approver_cannot_execute_own_ticket(tmp_path):
    _, _, store, approver, _, record, decision = setup(tmp_path)
    issued = issue(store, approver, record, decision)
    with pytest.raises(RetryAuthorizationError, match="different identities"):
        consume(store, approver, record, issued)


def test_ticket_cannot_cross_tenant(tmp_path):
    _, _, store, approver, _, record, decision = setup(tmp_path)
    issued = issue(store, approver, record, decision)
    other = principal("executor_2", tenant="tenant_other")
    with pytest.raises(RetryAuthorizationError, match="not found in this tenant"):
        consume(store, other, record, issued)


def test_only_retry_candidate_can_issue_and_ttl_is_bounded(tmp_path):
    _, _, store, approver, _, record, decision = setup(tmp_path)
    manual = decision.model_copy(update={"disposition": "manual_review"})
    with pytest.raises(RetryAuthorizationError, match="retry-candidate"):
        issue(store, approver, record, manual)
    with pytest.raises(ValueError, match="ttl_seconds"):
        issue(store, approver, record, decision, ttl=301)


def test_issuance_requires_approver_permission(tmp_path):
    _, _, store, _, _, record, decision = setup(tmp_path)
    read_only = principal("reviewer_1", {"workflow:read"})
    with pytest.raises(Exception):
        issue(store, read_only, record, decision)


def test_issuance_rejects_terminal_or_changed_durable_record(tmp_path):
    _, ledger, store, approver, _, _, decision = setup(tmp_path)
    key = hashlib.sha256(b"terminal-operation").hexdigest()
    terminal, created = ledger.reserve(
        "task_001:step_terminal", key, {"amount": 7}, principal=approver,
        occurred_at=160, authorization_now_epoch=160,
    )
    assert created
    ledger.mark_dispatching(key, principal=approver, occurred_at=161, authorization_now_epoch=161)
    terminal = ledger.mark_succeeded(
        key, {"ok": True}, principal=approver, occurred_at=162, authorization_now_epoch=162,
    )
    with pytest.raises(RetryAuthorizationError, match="binding or state"):
        issue(store, approver, terminal, decision, now=202)

def test_concurrent_consumption_has_exactly_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    _, _, store, approver, _, record, decision = setup(tmp_path)
    issued = issue(store, approver, record, decision)
    executors = [principal("executor_a"), principal("executor_b")]

    def attempt(executor):
        try:
            return ("consumed", consume(store, executor, record, issued, now=201).executor_id)
        except RetryAuthorizationError:
            return ("rejected", executor.subject_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, executors))

    assert sum(outcome[0] == "consumed" for outcome in outcomes) == 1
    assert sum(outcome[0] == "rejected" for outcome in outcomes) == 1
    with store.task_store._connection() as connection:
        row = connection.execute(
            "SELECT consumed_at, executor_id FROM cmx_retry_authorizations WHERE authorization_id=?",
            (issued.authorization_id,),
        ).fetchone()
    assert row["consumed_at"] == 201
    assert row["executor_id"] in {item.subject_id for item in executors}


def test_consumed_authorization_is_never_restored_after_execution_failure(tmp_path):
    _, _, store, approver, executor, record, decision = setup(tmp_path)
    issued = issue(store, approver, record, decision)
    consume(store, executor, record, issued)

    # Model a process/provider failure after the durable authorization transaction
    # commits. The ticket remains consumed; recovery must issue a new review and ticket.
    with pytest.raises(RetryAuthorizationError, match="already been consumed"):
        consume(store, executor, record, issued, now=202)

