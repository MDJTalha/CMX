import pytest
from pydantic import ValidationError

from cmx.kernel import (
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionPlan,
    InvalidTransitionError,
    PlanStep,
    StaleTaskVersionError,
    TaskEvent,
    TaskRecord,
    TaskState,
    next_task_state,
)


def capability(**overrides) -> CapabilitySpec:
    values = {
        "capability_id": "assessment.evaluate",
        "name": "Evaluate assessment",
        "description": "Evaluate a structured business assessment without external side effects.",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "required_permissions": ["assessment:read"],
        "timeout_seconds": 20,
        "max_retries": 1,
    }
    values.update(overrides)
    return CapabilitySpec(**values)


def plan(steps: list[PlanStep]) -> ExecutionPlan:
    return ExecutionPlan(plan_id="plan_001", objective="Evaluate a business assessment.", steps=steps)


def test_task_lifecycle_reaches_success_only_after_verification():
    task = TaskRecord(task_id="task_001", max_attempts=2)
    task = task.apply(TaskEvent.PLAN_ACCEPTED, expected_version=0)
    assert task.state == TaskState.PLANNED
    task = task.apply(TaskEvent.START, expected_version=1)
    assert task.state == TaskState.RUNNING
    task = task.apply(TaskEvent.BEGIN_VERIFICATION, expected_version=2)
    assert task.state == TaskState.VERIFYING
    task = task.apply(TaskEvent.VERIFICATION_PASSED, expected_version=3)
    assert task.state == TaskState.SUCCEEDED
    assert task.version == 4


def test_stale_version_is_rejected():
    task = TaskRecord(task_id="task_001")
    with pytest.raises(StaleTaskVersionError):
        task.apply(TaskEvent.PLAN_ACCEPTED, expected_version=1)


def test_invalid_transition_is_rejected():
    with pytest.raises(InvalidTransitionError):
        next_task_state(TaskState.CREATED, TaskEvent.START)


def test_terminal_state_cannot_be_reopened():
    with pytest.raises(InvalidTransitionError):
        next_task_state(TaskState.SUCCEEDED, TaskEvent.CANCEL)


def test_retry_is_bounded_and_increments_attempt_count():
    task = TaskRecord(task_id="task_001", state=TaskState.RUNNING, max_attempts=2)
    task = task.apply(TaskEvent.RETRY, expected_version=0)
    assert task.attempts == 2
    assert task.version == 1
    with pytest.raises(InvalidTransitionError):
        task.apply(TaskEvent.RETRY, expected_version=1)


def test_rejecting_approval_cancels_task():
    task = TaskRecord(task_id="task_001", state=TaskState.AWAITING_APPROVAL)
    result = task.apply(TaskEvent.REJECT, expected_version=0)
    assert result.state == TaskState.CANCELLED


def test_side_effecting_capability_requires_approval():
    with pytest.raises(ValidationError):
        capability(has_side_effects=True, approval_required=False)


def test_duplicate_capability_registration_is_rejected():
    registry = CapabilityRegistry()
    registry.register(capability())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(capability())


def test_unknown_or_disabled_capabilities_are_not_authorized():
    registry = CapabilityRegistry()
    with pytest.raises(KeyError, match="Unknown capability"):
        registry.get("vendor.execute")
    registry.register(capability(enabled=False))
    with pytest.raises(PermissionError, match="disabled"):
        registry.get("assessment.evaluate")


def test_capability_permission_requirements_are_enforced():
    registry = CapabilityRegistry()
    registry.register(capability())
    with pytest.raises(PermissionError, match="assessment:read"):
        registry.authorize("assessment.evaluate", set())
    assert registry.authorize("assessment.evaluate", {"assessment:read"}).capability_id == "assessment.evaluate"


def test_plan_rejects_unknown_dependencies():
    with pytest.raises(ValidationError, match="unknown dependencies"):
        plan([
            PlanStep(
                step_id="step_one",
                capability_id="assessment.evaluate",
                objective="Evaluate the request.",
                depends_on=["missing_step"],
            )
        ])


def test_plan_rejects_cycles():
    with pytest.raises(ValidationError, match="cycle"):
        plan([
            PlanStep(
                step_id="step_one",
                capability_id="assessment.evaluate",
                objective="Evaluate the request.",
                depends_on=["step_two"],
            ),
            PlanStep(
                step_id="step_two",
                capability_id="assessment.evaluate",
                objective="Verify the result.",
                depends_on=["step_one"],
            ),
        ])


def test_plan_rejects_duplicate_step_ids():
    with pytest.raises(ValidationError, match="unique"):
        plan([
            PlanStep(step_id="step_one", capability_id="assessment.evaluate", objective="Evaluate the request."),
            PlanStep(step_id="step_one", capability_id="assessment.evaluate", objective="Verify the result."),
        ])


def test_plan_enforces_capability_approval_and_retry_limits():
    registry = CapabilityRegistry()
    registry.register(capability(has_side_effects=True, approval_required=True, max_retries=0))
    unsafe_plan = plan([
        PlanStep(
            step_id="step_one",
            capability_id="assessment.evaluate",
            objective="Perform the approved external action.",
            requires_approval=False,
        )
    ])
    with pytest.raises(PermissionError, match="human approval"):
        registry.validate_plan(unsafe_plan, granted_permissions={"assessment:read"})

    too_many_retries = plan([
        PlanStep(
            step_id="step_one",
            capability_id="assessment.evaluate",
            objective="Perform the approved external action.",
            requires_approval=True,
            max_attempts=2,
        )
    ])
    with pytest.raises(ValueError, match="retry limit"):
        registry.validate_plan(too_many_retries, granted_permissions={"assessment:read"})


def test_plan_validates_permissions_without_executing_actions():
    registry = CapabilityRegistry()
    registry.register(capability())
    safe_plan = plan([
        PlanStep(
            step_id="step_one",
            capability_id="assessment.evaluate",
            objective="Evaluate the supplied business assessment.",
        )
    ])
    registry.validate_plan(safe_plan, granted_permissions={"assessment:read"})
    assert [item.capability_id for item in registry.list_capabilities()] == ["assessment.evaluate"]


def test_task_record_rejects_attempts_above_limit():
    with pytest.raises(ValidationError, match="cannot exceed"):
        TaskRecord(task_id="task_001", attempts=3, max_attempts=2)


from cmx.kernel import (
    CriterionResult,
    VerificationContract,
    VerificationCriterion,
    evaluate_verification,
)


def verification_contract() -> VerificationContract:
    return VerificationContract(
        contract_id="verify_001",
        objective="Verify the task result against acceptance criteria.",
        criteria=[
            VerificationCriterion(
                criterion_id="output_valid",
                description="The output matches the documented schema.",
                mandatory=True,
            ),
            VerificationCriterion(
                criterion_id="constraints_met",
                description="All mandatory user constraints are satisfied.",
                mandatory=True,
            ),
            VerificationCriterion(
                criterion_id="report_complete",
                description="An optional human-readable report is included.",
                mandatory=False,
            ),
        ],
    )


def test_verification_passes_only_when_all_mandatory_criteria_pass():
    decision = evaluate_verification(
        verification_contract(),
        [
            CriterionResult(criterion_id="output_valid", outcome="passed"),
            CriterionResult(criterion_id="constraints_met", outcome="passed"),
            CriterionResult(criterion_id="report_complete", outcome="unknown"),
        ],
    )
    assert decision.status == "passed"
    assert decision.unresolved_criteria == ["report_complete"]


def test_missing_mandatory_evidence_blocks_success():
    decision = evaluate_verification(
        verification_contract(),
        [CriterionResult(criterion_id="output_valid", outcome="passed")],
    )
    assert decision.status == "blocked"
    assert "constraints_met" in decision.unresolved_criteria


def test_failed_mandatory_criterion_fails_verification():
    decision = evaluate_verification(
        verification_contract(),
        [
            CriterionResult(criterion_id="output_valid", outcome="passed"),
            CriterionResult(criterion_id="constraints_met", outcome="failed"),
        ],
    )
    assert decision.status == "failed"
    assert decision.failed_criteria == ["constraints_met"]


def test_verification_rejects_duplicate_result_ids():
    with pytest.raises(ValueError, match="must be unique"):
        evaluate_verification(
            verification_contract(),
            [
                CriterionResult(criterion_id="output_valid", outcome="passed"),
                CriterionResult(criterion_id="output_valid", outcome="passed"),
            ],
        )


def test_verification_rejects_results_for_unknown_criteria():
    with pytest.raises(ValueError, match="unknown criteria"):
        evaluate_verification(
            verification_contract(),
            [CriterionResult(criterion_id="made_up", outcome="passed")],
        )


def test_verification_contract_requires_at_least_one_mandatory_criterion():
    with pytest.raises(ValidationError, match="At least one"):
        VerificationContract(
            contract_id="verify_002",
            objective="Verify a sample task.",
            criteria=[
                VerificationCriterion(
                    criterion_id="optional_check",
                    description="An optional check with no required outcome.",
                    mandatory=False,
                )
            ],
        )
