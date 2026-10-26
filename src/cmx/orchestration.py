"""Controlled, dependency-aware workflow orchestration for CMX.

The coordinator invokes only explicitly injected local handlers. It does not discover
or import tools dynamically. A handler adapter must enforce its supplied timeout and
must treat the provided idempotency key as a stable operation key where applicable.
A persisted step left running after a process interruption is never retried automatically.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cmx.kernel import (
    CapabilityRegistry,
    CriterionResult,
    ExecutionPlan,
    InvalidTransitionError,
    TaskEvent,
    TaskRecord,
    TaskState,
    VerificationContract,
    VerificationCriterion,
    evaluate_verification,
)
from cmx.persistence import SQLiteTaskStore
from cmx.schema_validation import CapabilityPayloadValidationError
from cmx.security import AuthorizationError, PrincipalContext, authorize_permissions
from cmx.workflow_store import (
    StepApprovalRequired,
    SQLiteWorkflowStore,
    WorkflowConflictError,
    WorkflowIntegrityError,
    WorkflowSnapshot,
    WorkflowStepRecord,
)


class ExecutionContext(BaseModel):
    """Small immutable context; it deliberately does not expose permission grants."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    task_id: str
    plan_id: str
    step_id: str
    capability_id: str
    capability_version: str
    attempt: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1, le=300)
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    plan_id: str
    task_state: Literal[
        "created", "planned", "running", "awaiting_approval", "verifying",
        "succeeded", "failed", "cancelled"
    ]
    workflow_state: Literal[
        "ready", "running", "awaiting_approval", "succeeded", "failed", "cancelled"
    ]
    status: Literal["running", "awaiting_approval", "succeeded", "failed", "cancelled"]
    awaiting_step_id: str | None = None
    failed_step_id: str | None = None
    completed_step_ids: tuple[str, ...] = ()
    message: str


CapabilityHandler = Callable[
    [dict[str, Any], dict[str, Any], ExecutionContext],
    Any,
]


class ExecutionCoordinator:
    """Run a stored plan step-by-step under task, tenant, permission and approval gates."""

    def __init__(
        self,
        *,
        task_store: SQLiteTaskStore,
        workflow_store: SQLiteWorkflowStore,
        registry: CapabilityRegistry,
        handlers: Mapping[str, CapabilityHandler] | None = None,
    ) -> None:
        if task_store.database_path != workflow_store.database_path:
            raise ValueError("Task and workflow stores must use the same database file.")
        self.task_store = task_store
        self.workflow_store = workflow_store
        self.registry = registry
        self.handlers: dict[str, CapabilityHandler] = {}
        for capability_id, handler in (handlers or {}).items():
            if not callable(handler):
                raise TypeError(f"Handler for capability '{capability_id}' must be callable.")
            self.handlers[capability_id] = handler

    @staticmethod
    def _clock(authorization_now_epoch: int | None, occurred_at: int | None = None) -> int | None:
        if occurred_at is not None:
            return occurred_at
        return authorization_now_epoch

    @staticmethod
    def _authorize_execution(
        principal: PrincipalContext | None,
        *,
        now_epoch: int | None,
    ) -> PrincipalContext:
        tenant = principal.tenant_id if isinstance(principal, PrincipalContext) else ""
        authorize_permissions(
            principal,
            required_permissions={
                "workflow:execute",
                "workflow:read",
                "tasks:read",
                "tasks:write",
            },
            resource_tenant_id=tenant,
            now_epoch=now_epoch,
        )
        if not isinstance(principal, PrincipalContext):
            raise AuthorizationError("unauthenticated", "A trusted authenticated identity is required.")
        return principal

    @staticmethod
    def _topological_order(plan: ExecutionPlan) -> list[str]:
        """Return a stable topological order, preserving plan order where possible."""
        step_ids = [step.step_id for step in plan.steps]
        remaining = {step.step_id: set(step.depends_on) for step in plan.steps}
        ordered: list[str] = []
        ready = [step_id for step_id in step_ids if not remaining[step_id]]
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for step_id in step_ids:
                dependencies = remaining[step_id]
                if current in dependencies:
                    dependencies.remove(current)
                    if not dependencies and step_id not in ordered and step_id not in ready:
                        ready.append(step_id)
        if len(ordered) != len(plan.steps):
            raise ValueError("Execution plan must be a directed acyclic graph.")
        return ordered

    @staticmethod
    def _outcome(snapshot: WorkflowSnapshot, *, message: str) -> ExecutionOutcome:
        step_by_id = {step.step_id: step for step in snapshot.steps}
        waiting = next(
            (step.step_id for step in snapshot.steps if step.status == "awaiting_approval"),
            None,
        )
        failed = next(
            (
                step.step_id for step in snapshot.steps
                if step.status in {"failed", "blocked", "interrupted"}
            ),
            None,
        )
        return ExecutionOutcome(
            task_id=snapshot.task.task_id,
            plan_id=snapshot.plan.plan_id,
            task_state=snapshot.task.state.value,
            workflow_state=snapshot.workflow_state,
            status=(
                "awaiting_approval" if waiting is not None
                else snapshot.task.state.value
                if snapshot.task.state.value in {"succeeded", "failed", "cancelled"}
                else "running"
            ),
            awaiting_step_id=waiting,
            failed_step_id=failed,
            completed_step_ids=tuple(
                step.step_id for step in snapshot.steps if step.status == "succeeded"
            ),
            message=message,
        )

    def submit(
        self,
        task_id: str,
        plan: ExecutionPlan,
        *,
        principal: PrincipalContext,
        max_task_attempts: int = 1,
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> WorkflowSnapshot:
        """Validate and durably submit a plan; no handler is called during submission."""
        self._authorize_execution(principal, now_epoch=authorization_now_epoch)
        self.registry.validate_plan(
            plan,
            principal=principal,
            resource_tenant_id=principal.tenant_id,
            now_epoch=authorization_now_epoch,
        )
        versions = {
            capability_id: self.registry.get(capability_id).version
            for capability_id in {step.capability_id for step in plan.steps}
        }
        task = TaskRecord(task_id=task_id, max_attempts=max_task_attempts)
        return self.workflow_store.create_workflow(
            task,
            plan,
            principal=principal,
            capability_versions=versions,
            occurred_at=self._clock(authorization_now_epoch, occurred_at),
            authorization_now_epoch=authorization_now_epoch,
        )

    def approve_step(
        self,
        task_id: str,
        step_id: str,
        *,
        approver: PrincipalContext,
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> WorkflowStepRecord:
        """Record a tenant-scoped human approval bound to the stored plan and step input."""
        return self.workflow_store.approve_step(
            task_id,
            step_id,
            principal=approver,
            occurred_at=self._clock(authorization_now_epoch, occurred_at),
            authorization_now_epoch=authorization_now_epoch,
        )

    def run_until_blocked(
        self,
        task_id: str,
        *,
        principal: PrincipalContext,
        authorization_now_epoch: int | None = None,
    ) -> ExecutionOutcome:
        """Run in topological order until success, explicit approval, or a safe failure."""
        identity = self._authorize_execution(principal, now_epoch=authorization_now_epoch)
        snapshot = self.workflow_store.get_workflow(
            task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
        )
        self.registry.validate_plan(
            snapshot.plan,
            principal=identity,
            resource_tenant_id=identity.tenant_id,
            now_epoch=authorization_now_epoch,
        )

        if snapshot.task.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
            desired = {
                TaskState.SUCCEEDED: "succeeded",
                TaskState.FAILED: "failed",
                TaskState.CANCELLED: "cancelled",
            }[snapshot.task.state]
            if snapshot.workflow_state != desired:
                self.workflow_store.set_workflow_state(
                    task_id, desired, principal=identity,
                    occurred_at=self._clock(authorization_now_epoch),
                    authorization_now_epoch=authorization_now_epoch,
                )
                snapshot = self.workflow_store.get_workflow(
                    task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
                )
            return self._outcome(snapshot, message="Workflow is already in a terminal state.")

        # Reconcile task lifecycle with a plan submission if a process stopped between writes.
        snapshot = self._reconcile_start(snapshot, identity, authorization_now_epoch)

        # Never retry a step that was persisted as running by a previous process. Its side
        # effect result may be uncertain; require a deliberate investigation/reconciliation.
        in_flight = next((step for step in snapshot.steps if step.status == "running"), None)
        if in_flight is not None:
            self.workflow_store.interrupt_step(
                task_id, in_flight.step_id, principal=identity,
                occurred_at=self._clock(authorization_now_epoch),
                authorization_now_epoch=authorization_now_epoch,
            )
            refreshed = self.workflow_store.get_workflow(
                task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
            )
            if refreshed.task.state == TaskState.RUNNING:
                self._apply_task_event(refreshed, TaskEvent.ERROR, identity, authorization_now_epoch)
            self.workflow_store.block_remaining_steps(
                task_id, principal=identity,
                occurred_at=self._clock(authorization_now_epoch),
                authorization_now_epoch=authorization_now_epoch,
            )
            snapshot = self.workflow_store.get_workflow(
                task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
            )
            return self._outcome(
                snapshot,
                message="An interrupted step has an uncertain result. Automatic replay was stopped for safety.",
            )

        snapshot = self.workflow_store.get_workflow(
            task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
        )
        if snapshot.workflow_state == "failed":
            if snapshot.task.state == TaskState.RUNNING:
                self._apply_task_event(snapshot, TaskEvent.ERROR, identity, authorization_now_epoch)
            refreshed = self.workflow_store.get_workflow(
                task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
            )
            return self._outcome(refreshed, message="Workflow is failed and requires investigation.")

        if snapshot.task.state == TaskState.AWAITING_APPROVAL:
            approved_waiting = next(
                (
                    step for step in snapshot.steps
                    if step.status == "pending" and step.requires_approval and step.approved_at is not None
                ),
                None,
            )
            if approved_waiting is None:
                return self._outcome(snapshot, message="Waiting for an authorized human approval.")
            snapshot = self._apply_task_event(
                snapshot, TaskEvent.APPROVE, identity, authorization_now_epoch
            )
            if snapshot.workflow_state == "awaiting_approval":
                self.workflow_store.set_workflow_state(
                    task_id, "running", principal=identity,
                    occurred_at=self._clock(authorization_now_epoch),
                    authorization_now_epoch=authorization_now_epoch,
                )
                snapshot = self.workflow_store.get_workflow(
                    task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
                )

        ordered_ids = self._topological_order(snapshot.plan)
        specs = {step.step_id: step for step in snapshot.plan.steps}

        for step_id in ordered_ids:
            snapshot = self.workflow_store.get_workflow(
                task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
            )
            step_by_id = {step.step_id: step for step in snapshot.steps}
            step = step_by_id[step_id]
            spec = specs[step_id]

            if step.status == "succeeded":
                continue
            if step.status in {"failed", "blocked", "interrupted"}:
                if snapshot.task.state == TaskState.RUNNING:
                    self._apply_task_event(snapshot, TaskEvent.ERROR, identity, authorization_now_epoch)
                self.workflow_store.block_remaining_steps(
                    task_id, principal=identity,
                    occurred_at=self._clock(authorization_now_epoch),
                    authorization_now_epoch=authorization_now_epoch,
                )
                failed_snapshot = self.workflow_store.get_workflow(
                    task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
                )
                return self._outcome(failed_snapshot, message="A step failed or was blocked; dependent work was not executed.")

            dependency_records = {dep: step_by_id[dep] for dep in spec.depends_on}
            if any(record.status != "succeeded" for record in dependency_records.values()):
                # Since steps are traversed topologically, an incomplete dependency normally
                # means it is waiting for approval or hit an earlier nonterminal boundary.
                waiting = next(
                    (record for record in dependency_records.values() if record.status == "awaiting_approval"),
                    None,
                )
                if waiting is not None:
                    return self._outcome(snapshot, message="A prerequisite is waiting for approval.")
                return self._outcome(snapshot, message="A prerequisite is not complete; downstream execution is paused.")

            if step.status == "awaiting_approval":
                if snapshot.task.state == TaskState.RUNNING:
                    self._apply_task_event(snapshot, TaskEvent.REQUIRE_APPROVAL, identity, authorization_now_epoch)
                waiting_snapshot = self.workflow_store.get_workflow(
                    task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
                )
                return self._outcome(waiting_snapshot, message="Waiting for an authorized human approval.")

            if step.requires_approval and step.approved_at is None:
                self.workflow_store.request_step_approval(
                    task_id, step_id, principal=identity,
                    occurred_at=self._clock(authorization_now_epoch),
                    authorization_now_epoch=authorization_now_epoch,
                )
                current = self.workflow_store.get_workflow(
                    task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
                )
                if current.task.state == TaskState.RUNNING:
                    self._apply_task_event(
                        current, TaskEvent.REQUIRE_APPROVAL, identity, authorization_now_epoch
                    )
                waiting_snapshot = self.workflow_store.get_workflow(
                    task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
                )
                return self._outcome(waiting_snapshot, message="Approval was requested; no handler was invoked.")

            if snapshot.task.state == TaskState.AWAITING_APPROVAL:
                snapshot = self._apply_task_event(
                    snapshot, TaskEvent.APPROVE, identity, authorization_now_epoch
                )

            handler = self.handlers.get(step.capability_id)
            if handler is None:
                self.workflow_store.fail_step(
                    task_id, step_id, error_code="handler_not_registered",
                    principal=identity, occurred_at=self._clock(authorization_now_epoch),
                    allow_pending=True, authorization_now_epoch=authorization_now_epoch,
                )
                current = self.workflow_store.get_workflow(
                    task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
                )
                if current.task.state == TaskState.RUNNING:
                    self._apply_task_event(current, TaskEvent.ERROR, identity, authorization_now_epoch)
                self.workflow_store.block_remaining_steps(
                    task_id, principal=identity,
                    occurred_at=self._clock(authorization_now_epoch),
                    authorization_now_epoch=authorization_now_epoch,
                )
                failed_snapshot = self.workflow_store.get_workflow(
                    task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
                )
                return self._outcome(failed_snapshot, message="No trusted handler is registered for a required capability.")

            started = self.workflow_store.start_step(
                task_id, step_id, principal=identity,
                occurred_at=self._clock(authorization_now_epoch),
                authorization_now_epoch=authorization_now_epoch,
            )
            capability = self.registry.get(step.capability_id)
            # JSON round-trips isolate handler-owned values from the immutable plan/snapshot.
            import json

            dependencies = {
                dep_id: json.loads(json.dumps(step_by_id[dep_id].output_payload, allow_nan=False))
                for dep_id in spec.depends_on
            }
            idempotency_key = hashlib.sha256(
                _stable_key(
                    identity.tenant_id,
                    task_id,
                    snapshot.plan.plan_id,
                    step_id,
                    capability.version,
                    spec.input_payload,
                ).encode("utf-8")
            ).hexdigest()
            context = ExecutionContext(
                tenant_id=identity.tenant_id,
                task_id=task_id,
                plan_id=snapshot.plan.plan_id,
                step_id=step_id,
                capability_id=capability.capability_id,
                capability_version=capability.version,
                attempt=started.attempts,
                timeout_seconds=capability.timeout_seconds,
                idempotency_key=idempotency_key,
            )
            try:
                output = handler(json.loads(json.dumps(spec.input_payload, allow_nan=False)), dependencies, context)
            except TimeoutError:
                self.workflow_store.fail_step(
                    task_id, step_id, error_code="handler_timeout",
                    principal=identity, occurred_at=self._clock(authorization_now_epoch),
                    authorization_now_epoch=authorization_now_epoch,
                )
                return self._finish_failure(task_id, identity, authorization_now_epoch, "A registered handler timed out.")
            except Exception:
                self.workflow_store.fail_step(
                    task_id, step_id, error_code="handler_error",
                    principal=identity, occurred_at=self._clock(authorization_now_epoch),
                    authorization_now_epoch=authorization_now_epoch,
                )
                return self._finish_failure(task_id, identity, authorization_now_epoch, "A registered handler failed; details were not persisted.")

            try:
                self.registry.validate_output(
                    step.capability_id,
                    output,
                    principal=identity,
                    resource_tenant_id=identity.tenant_id,
                    now_epoch=authorization_now_epoch,
                )
            except (CapabilityPayloadValidationError, ValueError):
                self.workflow_store.fail_step(
                    task_id, step_id, error_code="output_validation_failed",
                    principal=identity, occurred_at=self._clock(authorization_now_epoch),
                    authorization_now_epoch=authorization_now_epoch,
                )
                return self._finish_failure(task_id, identity, authorization_now_epoch, "Handler output did not satisfy its declared schema.")

            self.workflow_store.complete_step(
                task_id, step_id, output, principal=identity,
                occurred_at=self._clock(authorization_now_epoch),
                authorization_now_epoch=authorization_now_epoch,
            )

        return self._finish_success(task_id, identity, authorization_now_epoch)

    def _reconcile_start(
        self,
        snapshot: WorkflowSnapshot,
        principal: PrincipalContext,
        now_epoch: int | None,
    ) -> WorkflowSnapshot:
        current = snapshot
        if current.task.state == TaskState.CREATED:
            current = self._apply_task_event(current, TaskEvent.PLAN_ACCEPTED, principal, now_epoch)
        if current.task.state == TaskState.PLANNED:
            current = self._apply_task_event(current, TaskEvent.START, principal, now_epoch)
        if current.workflow_state == "ready" and current.task.state == TaskState.RUNNING:
            self.workflow_store.set_workflow_state(
                current.task.task_id, "running", principal=principal,
                occurred_at=self._clock(now_epoch), authorization_now_epoch=now_epoch,
            )
        return self.workflow_store.get_workflow(
            current.task.task_id, principal=principal, authorization_now_epoch=now_epoch
        )

    def _apply_task_event(
        self,
        snapshot: WorkflowSnapshot,
        event: TaskEvent,
        principal: PrincipalContext,
        now_epoch: int | None,
    ) -> WorkflowSnapshot:
        updated = self.task_store.apply_event(
            snapshot.task.task_id,
            event,
            principal=principal,
            expected_version=snapshot.task.version,
            occurred_at=self._clock(now_epoch),
            authorization_now_epoch=now_epoch,
        )
        return self.workflow_store.get_workflow(
            updated.task_id, principal=principal, authorization_now_epoch=now_epoch
        )

    def _finish_failure(
        self,
        task_id: str,
        principal: PrincipalContext,
        now_epoch: int | None,
        message: str,
    ) -> ExecutionOutcome:
        snapshot = self.workflow_store.get_workflow(
            task_id, principal=principal, authorization_now_epoch=now_epoch
        )
        if snapshot.task.state == TaskState.RUNNING:
            snapshot = self._apply_task_event(snapshot, TaskEvent.ERROR, principal, now_epoch)
        self.workflow_store.block_remaining_steps(
            task_id, principal=principal,
            occurred_at=self._clock(now_epoch),
            authorization_now_epoch=now_epoch,
        )
        final = self.workflow_store.get_workflow(
            task_id, principal=principal, authorization_now_epoch=now_epoch
        )
        return self._outcome(final, message=message)

    def _finish_success(
        self,
        task_id: str,
        principal: PrincipalContext,
        now_epoch: int | None,
    ) -> ExecutionOutcome:
        snapshot = self.workflow_store.get_workflow(
            task_id, principal=principal, authorization_now_epoch=now_epoch
        )
        if all(step.status == "succeeded" for step in snapshot.steps):
            if snapshot.task.state == TaskState.RUNNING:
                snapshot = self._apply_task_event(
                    snapshot, TaskEvent.BEGIN_VERIFICATION, principal, now_epoch
                )
            if snapshot.task.state == TaskState.VERIFYING:
                contract = VerificationContract(
                    contract_id=f"verify_{snapshot.plan.plan_id[:65]}",
                    objective="Verify that each workflow step produced schema-valid output.",
                    criteria=[
                        VerificationCriterion(
                            criterion_id=step.step_id,
                            description=f"Step {step.step_id} produced output accepted by its declared schema.",
                            mandatory=True,
                        )
                        for step in snapshot.plan.steps
                    ],
                )
                results = [
                    CriterionResult(criterion_id=step.step_id, outcome="passed")
                    for step in snapshot.plan.steps
                ]
                decision = evaluate_verification(contract, results)
                event = (
                    TaskEvent.VERIFICATION_PASSED
                    if decision.status == "passed"
                    else TaskEvent.VERIFICATION_FAILED
                )
                snapshot = self._apply_task_event(snapshot, event, principal, now_epoch)
            if snapshot.task.state == TaskState.SUCCEEDED:
                self.workflow_store.set_workflow_state(
                    task_id, "succeeded", principal=principal,
                    occurred_at=self._clock(now_epoch), authorization_now_epoch=now_epoch,
                )
                final = self.workflow_store.get_workflow(
                    task_id, principal=principal, authorization_now_epoch=now_epoch
                )
                return self._outcome(final, message="All workflow steps completed and all output schemas passed.")
        return self._outcome(snapshot, message="Workflow paused before final completion verification.")


def _stable_key(
    tenant_id: str,
    task_id: str,
    plan_id: str,
    step_id: str,
    capability_version: str,
    input_payload: Mapping[str, Any],
) -> str:
    """Canonical stable inputs for an idempotency key; contains no credentials."""
    import json

    return json.dumps(
        {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "plan_id": plan_id,
            "step_id": step_id,
            "capability_version": capability_version,
            "input_payload": input_payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
