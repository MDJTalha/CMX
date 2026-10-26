import pytest

from cmx.security import AuthorizationError
from cmx.worker_leases import SQLiteWorkerLeaseStore, WorkerLeaseConflict
from test_orchestration import make_plan, principal, stack, step, submit


def ready_workflow(tmp_path):
    task_store, workflow_store, _, coordinator = stack(tmp_path)
    submit(coordinator, make_plan([step("step_one")]))
    return task_store, workflow_store


def test_claim_is_persisted_and_blocks_second_worker_until_expiry(tmp_path):
    task_store, _ = ready_workflow(tmp_path)
    leases = SQLiteWorkerLeaseStore(task_store)
    first = leases.claim(
        "task_001", "step_one", worker_id="worker_a", lease_seconds=10,
        principal=principal(), now_epoch=200, authorization_now_epoch=200,
    )
    assert first.fencing_token == 1
    assert first.expires_at == 210
    with pytest.raises(WorkerLeaseConflict):
        leases.claim(
            "task_001", "step_one", worker_id="worker_b", lease_seconds=10,
            principal=principal(), now_epoch=209, authorization_now_epoch=209,
        )


def test_expired_lease_reclaim_increments_fencing_token_and_fences_old_owner(tmp_path):
    task_store, _ = ready_workflow(tmp_path)
    leases = SQLiteWorkerLeaseStore(task_store)
    old = leases.claim(
        "task_001", "step_one", worker_id="worker_a", lease_seconds=5,
        principal=principal(), now_epoch=200, authorization_now_epoch=200,
    )
    new = leases.claim(
        "task_001", "step_one", worker_id="worker_b", lease_seconds=5,
        principal=principal(), now_epoch=205, authorization_now_epoch=205,
    )
    assert new.fencing_token == old.fencing_token + 1
    with pytest.raises(WorkerLeaseConflict):
        leases.renew(old, lease_seconds=10, principal=principal(), now_epoch=205, authorization_now_epoch=205)
    with pytest.raises(WorkerLeaseConflict):
        leases.release(old, principal=principal(), now_epoch=205, authorization_now_epoch=205)


def test_lease_renewal_and_release_require_current_fencing_token(tmp_path):
    task_store, _ = ready_workflow(tmp_path)
    leases = SQLiteWorkerLeaseStore(task_store)
    lease = leases.claim(
        "task_001", "step_one", worker_id="worker_a", lease_seconds=5,
        principal=principal(), now_epoch=200, authorization_now_epoch=200,
    )
    renewed = leases.renew(
        lease, lease_seconds=20, principal=principal(), now_epoch=202, authorization_now_epoch=202,
    )
    assert renewed.expires_at == 222
    leases.release(renewed, principal=principal(), now_epoch=203, authorization_now_epoch=203)
    with pytest.raises(WorkerLeaseConflict):
        leases.renew(renewed, lease_seconds=5, principal=principal(), now_epoch=204, authorization_now_epoch=204)


def test_claim_is_tenant_scoped_and_rejects_non_pending_step(tmp_path):
    task_store, workflow_store = ready_workflow(tmp_path)
    leases = SQLiteWorkerLeaseStore(task_store)
    with pytest.raises(AuthorizationError):
        leases.claim(
            "task_001", "step_one", worker_id="worker_a", lease_seconds=5,
            principal=principal(tenant_id="tenant_other"), now_epoch=200, authorization_now_epoch=200,
        )
    workflow_store.start_step(
        "task_001", "step_one", principal=principal(), occurred_at=201, authorization_now_epoch=201,
    )
    with pytest.raises(WorkerLeaseConflict):
        leases.claim(
            "task_001", "step_one", worker_id="worker_a", lease_seconds=5,
            principal=principal(), now_epoch=202, authorization_now_epoch=202,
        )
