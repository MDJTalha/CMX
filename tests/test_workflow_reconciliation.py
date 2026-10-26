import pytest

from cmx.workflow_reconciliation import SQLiteWorkflowReconciler
from cmx.workflow_store import WorkflowConflictError
from tests.test_worker_leases import principal, ready_workflow


def test_reconciliation_requires_interrupted_step(tmp_path):
    _, store = ready_workflow(tmp_path)
    with pytest.raises(WorkflowConflictError, match="interrupted step"):
        SQLiteWorkflowReconciler(store).record_review(
            "task_001", "step_one", decision="inconclusive",
            evidence_reference="ticket:CMX-1", notes="Reviewed; still pending.",
            principal=principal(), occurred_at=210, authorization_now_epoch=210,
        )


def test_reconciliation_requires_privileged_identity(tmp_path):
    _, store = ready_workflow(tmp_path)
    restricted = principal()
    restricted = restricted.model_copy(update={"permissions": frozenset({"workflow:read", "tasks:read"})})
    with pytest.raises(Exception):
        SQLiteWorkflowReconciler(store).record_review(
            "task_001", "step_one", decision="inconclusive",
            evidence_reference="ticket:CMX-2", notes="Review required.",
            principal=restricted, occurred_at=210, authorization_now_epoch=210,
        )
