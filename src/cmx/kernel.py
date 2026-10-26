"""Bounded task lifecycle, execution-plan validation, and capability registry.

The kernel defines contracts only. It does not execute arbitrary tools or external actions.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskState(StrEnum):
    CREATED = "created"
    PLANNED = "planned"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskEvent(StrEnum):
    PLAN_ACCEPTED = "plan_accepted"
    START = "start"
    REQUIRE_APPROVAL = "require_approval"
    APPROVE = "approve"
    REJECT = "reject"
    BEGIN_VERIFICATION = "begin_verification"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"
    RETRY = "retry"
    ERROR = "error"
    CANCEL = "cancel"


TERMINAL_STATES = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}

_TRANSITIONS: dict[tuple[TaskState, TaskEvent], TaskState] = {
    (TaskState.CREATED, TaskEvent.PLAN_ACCEPTED): TaskState.PLANNED,
    (TaskState.CREATED, TaskEvent.CANCEL): TaskState.CANCELLED,
    (TaskState.PLANNED, TaskEvent.START): TaskState.RUNNING,
    (TaskState.PLANNED, TaskEvent.CANCEL): TaskState.CANCELLED,
    (TaskState.RUNNING, TaskEvent.REQUIRE_APPROVAL): TaskState.AWAITING_APPROVAL,
    (TaskState.RUNNING, TaskEvent.BEGIN_VERIFICATION): TaskState.VERIFYING,
    (TaskState.RUNNING, TaskEvent.ERROR): TaskState.FAILED,
    (TaskState.RUNNING, TaskEvent.CANCEL): TaskState.CANCELLED,
    (TaskState.AWAITING_APPROVAL, TaskEvent.APPROVE): TaskState.RUNNING,
    (TaskState.AWAITING_APPROVAL, TaskEvent.REJECT): TaskState.CANCELLED,
    (TaskState.AWAITING_APPROVAL, TaskEvent.CANCEL): TaskState.CANCELLED,
    (TaskState.VERIFYING, TaskEvent.VERIFICATION_PASSED): TaskState.SUCCEEDED,
    (TaskState.VERIFYING, TaskEvent.VERIFICATION_FAILED): TaskState.FAILED,
    (TaskState.VERIFYING, TaskEvent.CANCEL): TaskState.CANCELLED,
}


class InvalidTransitionError(ValueError):
    """Raised when an event is not permitted from the current state."""


class StaleTaskVersionError(ValueError):
    """Raised when a caller tries to mutate a task using an outdated version."""


def next_task_state(
    current: TaskState,
    event: TaskEvent,
    *,
    attempts: int = 1,
    max_attempts: int = 1,
) -> TaskState:
    """Return a permitted next state; retries are bounded and explicit."""
    if attempts < 1 or max_attempts < 1 or attempts > max_attempts:
        raise ValueError("attempts and max_attempts must satisfy 1 <= attempts <= max_attempts")

    if current == TaskState.RUNNING and event == TaskEvent.RETRY:
        if attempts >= max_attempts:
            raise InvalidTransitionError("Retry limit reached for this task.")
        return TaskState.RUNNING

    try:
        return _TRANSITIONS[(current, event)]
    except KeyError as exc:
        raise InvalidTransitionError(
            f"Event '{event.value}' is not permitted from state '{current.value}'."
        ) from exc


class TaskRecord(BaseModel):
    """Minimal versioned task state suitable for later persistent storage."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,79}$")
    state: TaskState = TaskState.CREATED
    version: int = Field(default=0, ge=0)
    attempts: int = Field(default=1, ge=1)
    max_attempts: int = Field(default=1, ge=1, le=5)
    last_event: TaskEvent | None = None

    @model_validator(mode="after")
    def attempts_are_bounded(self) -> "TaskRecord":
        if self.attempts > self.max_attempts:
            raise ValueError("attempts cannot exceed max_attempts")
        return self

    def apply(self, event: TaskEvent, *, expected_version: int) -> "TaskRecord":
        """Apply one transition with optimistic-concurrency protection."""
        if expected_version != self.version:
            raise StaleTaskVersionError(
                f"Expected task version {expected_version}, current version is {self.version}."
            )
        next_state = next_task_state(
            self.state,
            event,
            attempts=self.attempts,
            max_attempts=self.max_attempts,
        )
        next_attempts = self.attempts + 1 if event == TaskEvent.RETRY else self.attempts
        return self.model_copy(
            update={
                "state": next_state,
                "version": self.version + 1,
                "attempts": next_attempts,
                "last_event": event,
            }
        )


class CapabilitySpec(BaseModel):
    """Declarative capability contract; never implies that an executor is connected."""

    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,79}$")
    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(min_length=8, max_length=1000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    required_permissions: list[str] = Field(default_factory=list, max_length=20)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_retries: int = Field(default=0, ge=0, le=3)
    enabled: bool = True
    has_side_effects: bool = False
    approval_required: bool = False

    @model_validator(mode="after")
    def side_effects_require_approval(self) -> "CapabilitySpec":
        if self.has_side_effects and not self.approval_required:
            raise ValueError("Capabilities with side effects must require human approval.")
        if len(self.required_permissions) != len(set(self.required_permissions)):
            raise ValueError("required_permissions must be unique.")
        return self


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,39}$")
    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,79}$")
    objective: str = Field(min_length=5, max_length=500)
    depends_on: list[str] = Field(default_factory=list, max_length=30)
    input_payload: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    max_attempts: int = Field(default=1, ge=1, le=5)

    @model_validator(mode="after")
    def dependencies_are_unique_and_not_self_referential(self) -> "PlanStep":
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Step dependencies must be unique.")
        if self.step_id in self.depends_on:
            raise ValueError("A step cannot depend on itself.")
        return self


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,79}$")
    objective: str = Field(min_length=5, max_length=1000)
    steps: list[PlanStep] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def dependencies_form_a_dag(self) -> "ExecutionPlan":
        by_id = {step.step_id: step for step in self.steps}
        if len(by_id) != len(self.steps):
            raise ValueError("Step IDs must be unique.")
        for step in self.steps:
            unknown = set(step.depends_on) - set(by_id)
            if unknown:
                raise ValueError(
                    f"Step '{step.step_id}' has unknown dependencies: {', '.join(sorted(unknown))}."
                )

        # Kahn's algorithm: reject cyclic dependencies before any execution is possible.
        incoming = {step_id: set(step.depends_on) for step_id, step in by_id.items()}
        ready = [step_id for step_id, deps in incoming.items() if not deps]
        visited = 0
        while ready:
            current = ready.pop()
            visited += 1
            for step_id, deps in incoming.items():
                if current in deps:
                    deps.remove(current)
                    if not deps:
                        ready.append(step_id)
        if visited != len(by_id):
            raise ValueError("Plan dependencies contain a cycle.")
        return self


class CapabilityRegistry:
    """In-memory allowlist of declared capabilities and their security contracts."""

    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilitySpec] = {}

    def register(self, capability: CapabilitySpec) -> None:
        if capability.capability_id in self._capabilities:
            raise ValueError(f"Capability '{capability.capability_id}' is already registered.")
        self._capabilities[capability.capability_id] = capability

    def get(self, capability_id: str) -> CapabilitySpec:
        try:
            capability = self._capabilities[capability_id]
        except KeyError as exc:
            raise KeyError(f"Unknown capability '{capability_id}'.") from exc
        if not capability.enabled:
            raise PermissionError(f"Capability '{capability_id}' is disabled.")
        return capability

    def authorize(self, capability_id: str, granted_permissions: set[str]) -> CapabilitySpec:
        capability = self.get(capability_id)
        missing = set(capability.required_permissions) - granted_permissions
        if missing:
            raise PermissionError(
                f"Capability '{capability_id}' requires permissions: {', '.join(sorted(missing))}."
            )
        return capability

    def validate_plan(
        self,
        plan: ExecutionPlan,
        *,
        granted_permissions: set[str],
    ) -> None:
        """Check every step against the allowlist and permission set; execute nothing."""
        for step in plan.steps:
            capability = self.authorize(step.capability_id, granted_permissions)
            if capability.approval_required and not step.requires_approval:
                raise PermissionError(
                    f"Step '{step.step_id}' must explicitly require human approval."
                )
            if step.max_attempts > capability.max_retries + 1:
                raise ValueError(
                    f"Step '{step.step_id}' exceeds capability retry limit "
                    f"({capability.max_retries + 1} total attempts)."
                )

    def list_capabilities(self) -> list[CapabilitySpec]:
        """Return declarative metadata; no callable or external action is exposed."""
        return [self._capabilities[key] for key in sorted(self._capabilities)]
