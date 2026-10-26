"""Bounded task lifecycle, execution-plan validation, and capability registry.

The kernel defines contracts only. It does not execute arbitrary tools or external actions.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cmx.schema_validation import validate_json_payload, validate_json_schema
from cmx.security import PrincipalContext, authorize_permissions


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

    @field_validator("input_schema", "output_schema")
    @classmethod
    def schema_documents_are_valid(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_json_schema(value)
        return value

    @field_validator("required_permissions")
    @classmethod
    def permission_keys_are_valid(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"^[a-z][a-z0-9_.:-]{1,127}$", item) for item in value):
            raise ValueError("required_permissions contain an invalid permission key.")
        if len(value) != len(set(value)):
            raise ValueError("required_permissions must be unique.")
        return value

    @model_validator(mode="after")
    def side_effects_require_approval(self) -> "CapabilitySpec":
        if self.has_side_effects and not self.approval_required:
            raise ValueError("Capabilities with side effects must require human approval.")
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
    """In-memory capability allowlist governed by trusted tenant-scoped identity context."""

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

    def authorize(
        self,
        capability_id: str,
        *,
        principal: PrincipalContext | None,
        resource_tenant_id: str,
        now_epoch: int | None = None,
    ) -> CapabilitySpec:
        """Authorize against an internal trusted principal, its tenant and expiry."""
        capability = self.get(capability_id)
        authorize_permissions(
            principal,
            required_permissions=frozenset(capability.required_permissions),
            resource_tenant_id=resource_tenant_id,
            now_epoch=now_epoch,
        )
        return capability

    def validate_input(
        self,
        capability_id: str,
        payload: dict[str, Any],
        *,
        principal: PrincipalContext | None,
        resource_tenant_id: str,
        now_epoch: int | None = None,
    ) -> CapabilitySpec:
        """Authorize and validate input before any execution."""
        capability = self.authorize(
            capability_id,
            principal=principal,
            resource_tenant_id=resource_tenant_id,
            now_epoch=now_epoch,
        )
        validate_json_payload(
            capability.input_schema,
            payload,
            label=f"Input for capability '{capability_id}'",
        )
        return capability

    def validate_output(
        self,
        capability_id: str,
        payload: Any,
        *,
        principal: PrincipalContext | None,
        resource_tenant_id: str,
        now_epoch: int | None = None,
    ) -> CapabilitySpec:
        """Authorize and validate a result before it can be accepted."""
        capability = self.authorize(
            capability_id,
            principal=principal,
            resource_tenant_id=resource_tenant_id,
            now_epoch=now_epoch,
        )
        validate_json_payload(
            capability.output_schema,
            payload,
            label=f"Output for capability '{capability_id}'",
        )
        return capability

    def validate_plan(
        self,
        plan: ExecutionPlan,
        *,
        principal: PrincipalContext | None,
        resource_tenant_id: str,
        now_epoch: int | None = None,
    ) -> None:
        """Validate authorization and every step's input without executing anything."""
        for step in plan.steps:
            capability = self.authorize(
                step.capability_id,
                principal=principal,
                resource_tenant_id=resource_tenant_id,
                now_epoch=now_epoch,
            )
            validate_json_payload(
                capability.input_schema,
                step.input_payload,
                label=f"Input for plan step '{step.step_id}'",
            )
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


class VerificationCriterion(BaseModel):
    """A measurable completion condition that must be checked independently."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,39}$")
    description: str = Field(min_length=8, max_length=500)
    mandatory: bool = True


class VerificationContract(BaseModel):
    """Explicit, testable acceptance criteria for a task or plan."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,79}$")
    objective: str = Field(min_length=5, max_length=1000)
    criteria: list[VerificationCriterion] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def require_unique_ids_and_mandatory_criterion(self) -> "VerificationContract":
        ids = [criterion.criterion_id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("Verification criterion IDs must be unique.")
        if not any(criterion.mandatory for criterion in self.criteria):
            raise ValueError("At least one verification criterion must be mandatory.")
        return self


class CriterionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,39}$")
    outcome: Literal["passed", "failed", "unknown"]
    evidence_note: str | None = Field(default=None, max_length=1200)


class VerificationDecision(BaseModel):
    """Fail-closed verification summary derived from a contract and observed results."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    status: Literal["passed", "failed", "blocked"]
    passed_criteria: list[str] = Field(default_factory=list)
    failed_criteria: list[str] = Field(default_factory=list)
    unresolved_criteria: list[str] = Field(default_factory=list)
    rationale: str


def evaluate_verification(
    contract: VerificationContract,
    results: list[CriterionResult],
) -> VerificationDecision:
    """Evaluate completion evidence; missing or unknown mandatory checks block success."""
    result_by_id = {result.criterion_id: result for result in results}
    if len(result_by_id) != len(results):
        raise ValueError("Criterion result IDs must be unique.")

    allowed_ids = {criterion.criterion_id for criterion in contract.criteria}
    unexpected = set(result_by_id) - allowed_ids
    if unexpected:
        raise ValueError("Results contain unknown criteria: " + ", ".join(sorted(unexpected)) + ".")

    passed: list[str] = []
    failed: list[str] = []
    unresolved: list[str] = []
    mandatory_failed: list[str] = []
    mandatory_unresolved: list[str] = []

    for criterion in contract.criteria:
        result = result_by_id.get(criterion.criterion_id)
        outcome = result.outcome if result is not None else "unknown"
        if outcome == "passed":
            passed.append(criterion.criterion_id)
        elif outcome == "failed":
            failed.append(criterion.criterion_id)
            if criterion.mandatory:
                mandatory_failed.append(criterion.criterion_id)
        else:
            unresolved.append(criterion.criterion_id)
            if criterion.mandatory:
                mandatory_unresolved.append(criterion.criterion_id)

    if mandatory_failed:
        status: Literal["passed", "failed", "blocked"] = "failed"
        rationale = "One or more mandatory completion criteria failed; the task must not be marked successful."
    elif mandatory_unresolved:
        status = "blocked"
        rationale = "At least one mandatory completion criterion is missing or unknown; success is not established."
    else:
        status = "passed"
        rationale = "All mandatory completion criteria passed. Optional failures do not override the mandatory acceptance contract."

    return VerificationDecision(
        contract_id=contract.contract_id,
        status=status,
        passed_criteria=passed,
        failed_criteria=failed,
        unresolved_criteria=unresolved,
        rationale=rationale,
    )
