import hashlib

import pytest

from cmx.idempotency import IdempotencyConflict, SQLiteIdempotencyStore
from cmx.persistence import SQLiteTaskStore
from cmx.security import AuthorizationError
from test_worker_leases import principal


def key_for(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_store(tmp_path):
    tasks = SQLiteTaskStore(tmp_path / "idempotency.sqlite3")
    return tasks, SQLiteIdempotencyStore(tasks)


def test_reservation_is_durable_and_only_first_call_is_new(tmp_path):
    tasks, store = make_store(tmp_path)
    identity = principal()
    key = key_for("payment:create:task-1")
    record, created = store.reserve(
        "task_001:step_one", key, {"amount": 125, "currency": "PKR"},
        principal=identity, occurred_at=200, authorization_now_epoch=200,
    )
    duplicate, duplicate_created = store.reserve(
        "task_001:step_one", key, {"amount": 125, "currency": "PKR"},
        principal=identity, occurred_at=201, authorization_now_epoch=201,
    )
    assert created is True
    assert duplicate_created is False
    assert duplicate.operation_id == record.operation_id
    assert duplicate.state == "reserved"
    assert len(store.list_events(key, principal=identity, authorization_now_epoch=201)) == 1


def test_same_key_with_different_payload_is_rejected(tmp_path):
    _, store = make_store(tmp_path)
    identity = principal()
    key = key_for("same-operation")
    store.reserve("task_001:step_one", key, {"amount": 10},
                  principal=identity, occurred_at=200, authorization_now_epoch=200)
    with pytest.raises(IdempotencyConflict, match="different operation or payload"):
        store.reserve("task_001:step_one", key, {"amount": 11},
                      principal=identity, occurred_at=201, authorization_now_epoch=201)


def test_operation_state_machine_and_success_result_are_durable(tmp_path):
    _, store = make_store(tmp_path)
    identity = principal()
    key = key_for("operation-state")
    reserved, created = store.reserve(
        "task_001:step_one", key, {"input": "stable"},
        principal=identity, occurred_at=200, authorization_now_epoch=200,
    )
    assert created
    dispatching = store.mark_dispatching(
        key, principal=identity, occurred_at=201, authorization_now_epoch=201,
    )
    succeeded = store.mark_succeeded(
        key, {"provider_reference": "ref-123", "status": "accepted"},
        principal=identity, occurred_at=202, authorization_now_epoch=202,
    )
    assert reserved.state == "reserved"
    assert dispatching.state == "dispatching"
    assert succeeded.state == "succeeded"
    assert store.get(key, principal=identity, authorization_now_epoch=203).result == {
        "provider_reference": "ref-123", "status": "accepted"
    }
    events = store.list_events(key, principal=identity, authorization_now_epoch=203)
    assert [event.event_type for event in events] == [
        "operation.reserved", "operation.dispatching", "operation.succeeded"
    ]


def test_duplicate_dispatch_and_invalid_state_transitions_are_rejected(tmp_path):
    _, store = make_store(tmp_path)
    identity = principal()
    key = key_for("no-repeat")
    store.reserve("task_001:step_one", key, {"x": 1},
                  principal=identity, occurred_at=200, authorization_now_epoch=200)
    store.mark_dispatching(key, principal=identity, occurred_at=201, authorization_now_epoch=201)
    with pytest.raises(IdempotencyConflict, match="Cannot transition"):
        store.mark_dispatching(key, principal=identity, occurred_at=202, authorization_now_epoch=202)
    store.mark_uncertain(key, principal=identity, occurred_at=203, authorization_now_epoch=203)
    with pytest.raises(IdempotencyConflict, match="Cannot transition"):
        store.mark_dispatching(key, principal=identity, occurred_at=204, authorization_now_epoch=204)


def test_tenant_cannot_read_or_transition_another_tenants_operation(tmp_path):
    _, store = make_store(tmp_path)
    owner = principal()
    key = key_for("tenant-bound")
    store.reserve("task_001:step_one", key, {"x": 1},
                  principal=owner, occurred_at=200, authorization_now_epoch=200)
    other = owner.model_copy(update={"tenant_id": "tenant_other"})
    with pytest.raises(LookupError, match="not found"):
        store.get(key, principal=other, authorization_now_epoch=201)
    with pytest.raises(LookupError, match="not found"):
        store.mark_dispatching(key, principal=other, occurred_at=201, authorization_now_epoch=201)
    assert store.get(key, principal=owner, authorization_now_epoch=201).state == "reserved"


def test_idempotency_store_enforces_permissions(tmp_path):
    _, store = make_store(tmp_path)
    identity = principal().model_copy(update={
        "permissions": frozenset(p for p in principal().permissions if p != "workflow:execute")
    })
    with pytest.raises(AuthorizationError):
        store.reserve("task_001:step_one", key_for("permission"), {"x": 1},
                      principal=identity, occurred_at=200, authorization_now_epoch=200)


def test_uncertain_operation_is_never_reopened_by_duplicate_reservation(tmp_path):
    _, store = make_store(tmp_path)
    identity = principal()
    key = key_for("uncertain-do-not-replay")
    store.reserve("task_001:step_one", key, {"x": 1},
                  principal=identity, occurred_at=200, authorization_now_epoch=200)
    store.mark_dispatching(key, principal=identity, occurred_at=201, authorization_now_epoch=201)
    store.mark_uncertain(key, principal=identity, occurred_at=202, authorization_now_epoch=202)
    record, created = store.reserve("task_001:step_one", key, {"x": 1},
                                    principal=identity, occurred_at=203, authorization_now_epoch=203)
    assert created is False
    assert record.state == "uncertain"
    assert [event.event_type for event in store.list_events(
        key, principal=identity, authorization_now_epoch=203
    )] == ["operation.reserved", "operation.dispatching", "operation.uncertain"]


def test_idempotency_audit_chain_detects_mutation(tmp_path):
    tasks, store = make_store(tmp_path)
    identity = principal()
    key = key_for("audit-tamper")
    store.reserve("task_001:step_one", key, {"x": 1},
                  principal=identity, occurred_at=200, authorization_now_epoch=200)
    with tasks._connection() as connection:
        connection.execute(
            "UPDATE cmx_idempotency_events SET payload_json = ? WHERE tenant_id = ? AND idempotency_key = ?",
            ('{"changed":true}', identity.tenant_id, key),
        )
    from cmx.idempotency import IdempotencyIntegrityError
    with pytest.raises(IdempotencyIntegrityError, match="integrity verification"):
        store.list_events(key, principal=identity, authorization_now_epoch=201)
