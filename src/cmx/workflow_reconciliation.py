"""Operator-recorded reconciliation reviews for uncertain workflow outcomes.

This module records evidence-backed decisions without replaying handlers or silently
rewriting terminal workflow state. It deliberately remains audit-only.
"""
from __future__ import annotations
from typing import Any, Literal

from cmx.kernel import CapabilityRegistry
from cmx.schema_validation import validate_json_payload
from cmx.persistence import _timestamp, _validate_identifier
from cmx.security import PrincipalContext
from cmx.workflow_store import SQLiteWorkflowStore, WorkflowConflictError, WorkflowIntegrityError, WorkflowNotFoundError

ReconciliationDecision = Literal["effect_confirmed", "effect_not_applied", "inconclusive"]


class SQLiteWorkflowReconciler:
    """Record an authorized review of an interrupted step without replaying it."""

    def __init__(self, workflow_store: SQLiteWorkflowStore) -> None:
        self.workflow_store = workflow_store
        self.task_store = workflow_store.task_store


    def inspect_uncertain_steps(
        self,
        task_id: str,
        *,
        principal: PrincipalContext,
        authorization_now_epoch: int | None = None,
    ) -> list[dict[str, object]]:
        """Return a tenant-scoped inventory without changing workflow state or invoking handlers.

        A running step with a live lease is omitted because it is still owned by a worker.
        Interrupted steps are always visible, but are marked non-actionable while a lease
        remains live. The latest recorded reconciliation review is included when present.
        """
        identity = self.workflow_store._authorize(
            principal, {"workflow:read", "tasks:read", "audit:read"},
            authorization_now_epoch,
        )
        _validate_identifier(task_id, "task_id")
        now_epoch = _timestamp(authorization_now_epoch)
        inventory: list[dict[str, object]] = []
        with self.task_store._connection() as connection:
            self.workflow_store._assert_workflow_integrity(connection, identity.tenant_id, task_id)
            plan_row = self.workflow_store._plan_row(connection, identity.tenant_id, task_id)
            if plan_row is None:
                raise WorkflowNotFoundError("Workflow was not found in the active tenant.")
            steps = connection.execute(
                """SELECT step_id, status, error_code, updated_at
                   FROM cmx_workflow_steps
                   WHERE tenant_id = ? AND task_id = ?
                     AND status IN ('running', 'interrupted')
                   ORDER BY ordinal""",
                (identity.tenant_id, task_id),
            ).fetchall()
            events = self.workflow_store._event_rows(connection, identity.tenant_id, task_id)
            reviews_by_step: dict[str, dict[str, object]] = {}
            for event in events:
                if event["event_type"] != "step.reconciliation_recorded":
                    continue
                payload = self.workflow_store._decode_event_payload(event)
                step_id = payload.get("step_id")
                details = payload.get("details")
                if isinstance(step_id, str) and isinstance(details, dict):
                    reviews_by_step[step_id] = {
                        "event_id": event["event_id"],
                        "occurred_at": event["occurred_at"],
                        "recorded_by": event["actor_id"],
                        "decision": details.get("decision"),
                        "evidence_reference": details.get("evidence_reference"),
                        "notes": details.get("notes"),
                    }
            for step in steps:
                lease = connection.execute(
                    """SELECT expires_at FROM cmx_worker_leases
                       WHERE tenant_id = ? AND task_id = ? AND step_id = ?""",
                    (identity.tenant_id, task_id, step["step_id"]),
                ).fetchone()
                lease_state = (
                    "missing" if lease is None
                    else "active" if lease["expires_at"] > now_epoch
                    else "expired"
                )
                if step["status"] == "running" and lease_state == "active":
                    continue
                inventory.append({
                    "task_id": task_id,
                    "step_id": step["step_id"],
                    "step_status": step["status"],
                    "error_code": step["error_code"],
                    "updated_at": step["updated_at"],
                    "lease_state": lease_state,
                    "actionable": lease_state != "active",
                    "latest_review": reviews_by_step.get(step["step_id"]),
                    "automatic_replay": False,
                })
        return inventory


    def validate_success_resolution(
        self,
        task_id: str,
        step_id: str,
        *,
        output_payload: Any,
        registry: CapabilityRegistry,
        principal: PrincipalContext,
        authorization_now_epoch: int | None = None,
    ) -> dict[str, object]:
        """Validate a proposed successful resolution without committing workflow state.

        This is a preflight only. It requires the latest review to confirm the external
        effect, a non-live lease, and an unchanged pinned capability contract. It never
        writes the supplied output or marks a step successful.
        """
        identity = self.workflow_store._authorize(
            principal, {"workflow:reconcile", "workflow:read", "tasks:read", "audit:read"},
            authorization_now_epoch,
        )
        _validate_identifier(task_id, "task_id")
        _validate_identifier(step_id, "step_id")
        now_epoch = _timestamp(authorization_now_epoch)
        with self.task_store._connection() as connection:
            self.workflow_store._assert_workflow_integrity(connection, identity.tenant_id, task_id)
            plan_row = self.workflow_store._plan_row(connection, identity.tenant_id, task_id)
            if plan_row is None:
                raise WorkflowNotFoundError("Workflow was not found in the active tenant.")
            step_row = connection.execute(
                """SELECT * FROM cmx_workflow_steps
                   WHERE tenant_id = ? AND task_id = ? AND step_id = ?""",
                (identity.tenant_id, task_id, step_id),
            ).fetchone()
            if step_row is None:
                raise WorkflowNotFoundError("Workflow step was not found in the active tenant.")
            if step_row["status"] != "interrupted":
                raise WorkflowConflictError("Only an interrupted step can be preflighted for success resolution.")
            lease = connection.execute(
                """SELECT expires_at FROM cmx_worker_leases
                   WHERE tenant_id = ? AND task_id = ? AND step_id = ?""",
                (identity.tenant_id, task_id, step_id),
            ).fetchone()
            if lease is not None and lease["expires_at"] > now_epoch:
                raise WorkflowConflictError("A live worker lease exists; success resolution is blocked.")
            event_rows = self.workflow_store._event_rows(connection, identity.tenant_id, task_id)
            reviews = []
            for event in event_rows:
                if event["event_type"] != "step.reconciliation_recorded":
                    continue
                payload = self.workflow_store._decode_event_payload(event)
                if payload.get("step_id") != step_id:
                    continue
                details = payload.get("details")
                if isinstance(details, dict):
                    reviews.append(details)
            if not reviews or reviews[-1].get("decision") != "effect_confirmed":
                raise WorkflowConflictError(
                    "The latest reconciliation review must confirm the external effect before success preflight."
                )
            capability_id = step_row["capability_id"]
            pinned_version = step_row["capability_version"]
            pinned_fingerprint = step_row["capability_fingerprint"]

        capability = registry.get(capability_id)
        from cmx.orchestration import _capability_fingerprint
        if capability.version != pinned_version or _capability_fingerprint(capability) != pinned_fingerprint:
            raise WorkflowConflictError(
                "The registered capability no longer matches the pinned contract; success resolution is blocked."
            )
        validate_json_payload(
            capability.output_schema, output_payload, label="Reconciled capability output"
        )
        return {
            "task_id": task_id,
            "step_id": step_id,
            "capability_id": capability_id,
            "capability_version": pinned_version,
            "output_payload": output_payload,
            "validated": True,
            "committed": False,
            "workflow_state_changed": False,
            "automatic_replay": False,
        }


    def validate_retry_candidate(
        self,
        task_id: str,
        step_id: str,
        *,
        registry: CapabilityRegistry,
        principal: PrincipalContext,
        authorization_now_epoch: int | None = None,
    ) -> dict[str, object]:
        """Check retry preconditions without authorizing or replaying an operation."""
        identity = self.workflow_store._authorize(
            principal, {"workflow:reconcile", "workflow:execute", "workflow:read",
                        "tasks:read", "audit:read"}, authorization_now_epoch,
        )
        _validate_identifier(task_id, "task_id")
        _validate_identifier(step_id, "step_id")
        now_epoch = _timestamp(authorization_now_epoch)
        with self.task_store._connection() as connection:
            self.workflow_store._assert_workflow_integrity(connection, identity.tenant_id, task_id)
            plan_row = self.workflow_store._plan_row(connection, identity.tenant_id, task_id)
            if plan_row is None:
                raise WorkflowNotFoundError("Workflow was not found in the active tenant.")
            row = connection.execute(
                """SELECT * FROM cmx_workflow_steps
                   WHERE tenant_id = ? AND task_id = ? AND step_id = ?""",
                (identity.tenant_id, task_id, step_id),
            ).fetchone()
            if row is None:
                raise WorkflowNotFoundError("Workflow step was not found in the active tenant.")
            if row["status"] != "interrupted":
                raise WorkflowConflictError("Only an interrupted step can be checked for retry candidacy.")
            lease = connection.execute(
                """SELECT expires_at FROM cmx_worker_leases
                   WHERE tenant_id = ? AND task_id = ? AND step_id = ?""",
                (identity.tenant_id, task_id, step_id),
            ).fetchone()
            if lease is not None and lease["expires_at"] > now_epoch:
                raise WorkflowConflictError("A live worker lease exists; retry candidacy is blocked.")
            latest_review = None
            for event in self.workflow_store._event_rows(connection, identity.tenant_id, task_id):
                if event["event_type"] != "step.reconciliation_recorded":
                    continue
                payload = self.workflow_store._decode_event_payload(event)
                if payload.get("step_id") == step_id and isinstance(payload.get("details"), dict):
                    latest_review = payload["details"]
            if not isinstance(latest_review, dict) or latest_review.get("decision") != "effect_not_applied":
                raise WorkflowConflictError(
                    "The latest reconciliation review must confirm the effect was not applied before retry candidacy."
                )
            if row["attempts"] >= row["max_attempts"]:
                raise WorkflowConflictError("Step retry limit has been reached.")
            plan = ExecutionPlan.model_validate_json(plan_row["plan_json"])
            spec = next((item for item in plan.steps if item.step_id == step_id), None)
            if spec is None:
                raise WorkflowIntegrityError("Step is absent from the stored immutable plan.")
            statuses = {
                item["step_id"]: item["status"]
                for item in connection.execute(
                    "SELECT step_id, status FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ?",
                    (identity.tenant_id, task_id),
                ).fetchall()
            }
            if any(statuses.get(dep) != "succeeded" for dep in spec.depends_on):
                raise WorkflowConflictError("Retry candidacy requires every dependency to have succeeded.")
            capability_id = row["capability_id"]
            pinned_version = row["capability_version"]
            pinned_fingerprint = row["capability_fingerprint"]
            attempts = row["attempts"]
            max_attempts = row["max_attempts"]
            requires_approval = bool(row["requires_approval"])
            approved = row["approved_by"] is not None and row["approved_at"] is not None and row["approval_fingerprint"] is not None
        capability = registry.get(capability_id)
        from cmx.orchestration import _capability_fingerprint
        if capability.version != pinned_version or _capability_fingerprint(capability) != pinned_fingerprint:
            raise WorkflowConflictError(
                "The registered capability no longer matches the pinned contract; retry candidacy is blocked."
            )
        if requires_approval and not approved:
            raise WorkflowConflictError(
                "Retry candidacy requires a retained approval; request fresh approval before retry."
            )
        return {
            "task_id": task_id, "step_id": step_id, "attempts": attempts,
            "max_attempts": max_attempts, "candidate": True, "committed": False,
            "workflow_state_changed": False, "automatic_replay": False,
            "retry_authorized": False,
        }

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
        # Lease safety must use the trusted current authorization clock, not a caller-supplied
        # historical audit timestamp. Otherwise a stale timestamp could bypass a live lease.
        lease_check_epoch = _timestamp(authorization_now_epoch)
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
                if lease is not None and lease["expires_at"] > lease_check_epoch:
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
