"""Operator-recorded reconciliation reviews for uncertain workflow outcomes.

This module records evidence-backed decisions without replaying handlers or silently
rewriting terminal workflow state. It deliberately remains audit-only.
"""
from __future__ import annotations
from typing import Literal

from cmx.persistence import _timestamp, _validate_identifier
from cmx.security import PrincipalContext
from cmx.workflow_store import SQLiteWorkflowStore, WorkflowConflictError, WorkflowNotFoundError

ReconciliationDecision = Literal["effect_confirmed", "effect_not_applied", "inconclusive"]


class SQLiteWorkflowReconciler:
    """Record an authorized review of an interrupted step without replaying it."""

    def __init__(self, workflow_store: SQLiteWorkflowStore) -> None:
        self.workflow_store = workflow_store
        self.task_store = workflow_store.task_store

    def record_review(
        self, task_id: str, step_id: str, *,
        decision: ReconciliationDecision, evidence_reference: str, notes: str,
        principal: PrincipalContext, occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> dict[str, object]:
        """Append a review event; evidence must come from an independently checked source."""
        identity = self.workflow_store._authorize(
            principal, {"workflow:reconcile", "workflow:read", "audit:write"},
            authorization_now_epoch,
        )
        _validate_identifier(task_id, "task_id")
        _validate_identifier(step_id, "step_id")
        if decision not in {"effect_confirmed", "effect_not_applied", "inconclusive"}:
            raise ValueError("decision must be a supported reconciliation decision.")
        if not isinstance(evidence_reference, str) or not evidence_reference.strip():
            raise ValueError("evidence_reference is required.")
        if len(evidence_reference.strip()) > 512:
            raise ValueError("evidence_reference must not exceed 512 characters.")
        if not isinstance(notes, str) or not notes.strip():
            raise ValueError("notes are required.")
        if len(notes.strip()) > 2000:
            raise ValueError("notes must not exceed 2000 characters.")
        timestamp = _timestamp(occurred_at)
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self.workflow_store._assert_workflow_integrity(connection, identity.tenant_id, task_id)
                plan_row = self.workflow_store._plan_row(connection, identity.tenant_id, task_id)
                row = connection.execute(
                    """SELECT status FROM cmx_workflow_steps
                       WHERE tenant_id = ? AND task_id = ? AND step_id = ?""",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                if plan_row is None or row is None:
                    raise WorkflowNotFoundError("Workflow step was not found in the active tenant.")
                if row["status"] != "interrupted":
                    raise WorkflowConflictError(
                        "Reconciliation review can only be recorded for an interrupted step."
                    )
                lease = connection.execute(
                    """SELECT expires_at FROM cmx_worker_leases
                       WHERE tenant_id = ? AND task_id = ? AND step_id = ?""",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                if lease is not None and lease["expires_at"] > timestamp:
                    raise WorkflowConflictError("A live worker lease exists; reconciliation review is blocked.")
                self.workflow_store._record_step_event(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    actor_id=identity.subject_id, occurred_at=timestamp,
                    event_type="step.reconciliation_recorded", workflow_state=plan_row["state"],
                    step_id=step_id,
                    extra_payload={
                        "decision": decision,
                        "evidence_reference": evidence_reference.strip(),
                        "notes": notes.strip(),
                        "automatic_replay": False,
                        "workflow_state_changed": False,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "task_id": task_id, "step_id": step_id, "decision": decision,
            "evidence_reference": evidence_reference.strip(), "recorded_by": identity.subject_id,
            "occurred_at": timestamp, "automatic_replay": False, "workflow_state_changed": False,
        }
