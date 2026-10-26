import pytest

from cmx.workflow_reconciliation import SQLiteWorkflowReconciler
from cmx.workflow_store import WorkflowConflictError
from cmx.security import AuthorizationError
from cmx.worker_leases import SQLiteWorkerLeaseStore
from test_worker_leases import principal, ready_workflow


def reviewer():
    identity = principal()
    return identity.model_copy(update={
        "permissions": identity.permissions | frozenset({"workflow:reconcile", "audit:write", "audit:read"})
    })


def interrupted_store(tmp_path, *, lease_seconds=5):
    task_store, store = ready_workflow(tmp_path)
    identity = reviewer()
    leases = SQLiteWorkerLeaseStore(task_store)
    lease = leases.claim(
        "task_001", "step_one", worker_id="worker_recovered",
        lease_seconds=lease_seconds, principal=identity, now_epoch=200, authorization_now_epoch=200,
    )
    store.start_step(
        "task_001", "step_one", principal=identity, occurred_at=201,
        authorization_now_epoch=201, worker_id=lease.worker_id,
        fencing_token=lease.fencing_token,
    )
    store.interrupt_step(
        "task_001", "step_one", principal=identity,
        occurred_at=206, authorization_now_epoch=206,
    )
    return store, identity


def test_reconciliation_records_evidence_without_replay_or_state_rewrite(tmp_path):
    store, identity = interrupted_store(tmp_path)
    before = store.get_workflow("task_001", principal=identity, authorization_now_epoch=210)
    result = SQLiteWorkflowReconciler(store).record_review(
        "task_001", "step_one", decision="effect_confirmed",
        evidence_reference="ticket:CMX-1042",
        notes="External ledger entry independently verified by the on-call operator.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    after = store.get_workflow("task_001", principal=identity, authorization_now_epoch=210)
    events = store.list_workflow_events(
        "task_001", principal=identity, authorization_now_epoch=210,
    )
    review = events[-1]
    assert result["automatic_replay"] is False
    assert result["workflow_state_changed"] is False
    assert before.steps[0].status == after.steps[0].status == "interrupted"
    assert before.workflow_state == after.workflow_state
    assert review.event_type == "step.reconciliation_recorded"
    assert review.payload["details"]["decision"] == "effect_confirmed"
    assert review.payload["details"]["evidence_reference"] == "ticket:CMX-1042"
    assert review.payload["details"]["automatic_replay"] is False


def test_reconciliation_requires_explicit_permission(tmp_path):
    store, _ = interrupted_store(tmp_path)
    restricted = principal()
    with pytest.raises(AuthorizationError):
        SQLiteWorkflowReconciler(store).record_review(
            "task_001", "step_one", decision="inconclusive",
            evidence_reference="ticket:CMX-1", notes="Investigated.",
            principal=restricted, occurred_at=210, authorization_now_epoch=210,
        )


@pytest.mark.parametrize("evidence,notes", [("", "Reviewed"), ("ticket:1", ""), ("  ", "Reviewed")])
def test_reconciliation_requires_evidence_reference_and_notes(tmp_path, evidence, notes):
    store, identity = interrupted_store(tmp_path)
    with pytest.raises(ValueError):
        SQLiteWorkflowReconciler(store).record_review(
            "task_001", "step_one", decision="inconclusive",
            evidence_reference=evidence, notes=notes,
            principal=identity, occurred_at=210, authorization_now_epoch=210,
        )


def test_reconciliation_rejects_non_interrupted_step(tmp_path):
    _, store = ready_workflow(tmp_path)
    identity = reviewer()
    with pytest.raises(WorkflowConflictError, match="interrupted step"):
        SQLiteWorkflowReconciler(store).record_review(
            "task_001", "step_one", decision="inconclusive",
            evidence_reference="ticket:CMX-2", notes="Not interrupted.",
            principal=identity, occurred_at=210, authorization_now_epoch=210,
        )



def test_future_audit_timestamp_cannot_bypass_live_lease(tmp_path):
    store, identity = interrupted_store(tmp_path, lease_seconds=100)
    with pytest.raises(WorkflowConflictError, match="live worker lease"):
        SQLiteWorkflowReconciler(store).record_review(
            "task_001", "step_one", decision="inconclusive",
            evidence_reference="ticket:CMX-3", notes="Attempted with a future audit timestamp.",
            principal=identity, occurred_at=400, authorization_now_epoch=210,
        )
