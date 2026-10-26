import hashlib
import sqlite3

import pytest
from pydantic import ValidationError

from cmx.kernel import (
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionPlan,
    PlanStep,
    TaskEvent,
    TaskRecord,
    TaskState,
)
from cmx.orchestration import ExecutionCoordinator, ExecutionContext, _capability_fingerprint, _stable_key
from cmx.persistence import SQLiteTaskStore
from cmx.security import AuthorizationError, PrincipalContext
from cmx.workflow_store import (
    StepApprovalRequired,
    SQLiteWorkflowStore,
    WorkflowIntegrityError,
    WorkflowNotFoundError,
)


BASE_PERMISSIONS = frozenset(
    {
        "workflow:submit",
        "workflow:execute",
        "workflow:read",
        "workflow:approve",
        "tasks:create",
        "tasks:read",
        "tasks:write",
        "audit:read",
        "demo:execute",
        "demo:send",
    }
)


def principal(
    *,
    subject_id: str = "user_001",
    tenant_id: str = "tenant_alpha",
    permissions: frozenset[str] = BASE_PERMISSIONS,
    issued_at: int = 100,
    expires_at: int = 1000,
    verified: bool = True,
) -> PrincipalContext:
    return PrincipalContext(
        subject_id=subject_id,
        tenant_id=tenant_id,
        issuer="https://identity.cmx.test",
        permissions=permissions,
        issued_at=issued_at,
        expires_at=expires_at,
        authentication_verified=verified,
        authentication_method="test",
        credential_id=f"cred_{subject_id}",
    )


def capability(**overrides) -> CapabilitySpec:
    values = {
        "capability_id": "demo.transform",
        "name": "Transform demo value",
        "description": "Transform a value for a controlled workflow test.",
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"result": {"type": "number"}},
            "required": ["result"],
            "additionalProperties": False,
        },
        "required_permissions": ["demo:execute"],
        "timeout_seconds": 10,
        "max_retries": 0,
    }
    values.update(overrides)
    return CapabilitySpec(**values)


def make_plan(steps: list[PlanStep], *, plan_id: str = "plan_001") -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        objective="Run a tested, bounded CMX workflow.",
        steps=steps,
    )


def stack(tmp_path, *, handlers=None, specs=None):
    task_store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    workflow_store = SQLiteWorkflowStore(task_store)
    registry = CapabilityRegistry()
    for spec in specs or [capability()]:
        registry.register(spec)
    coordinator = ExecutionCoordinator(
        task_store=task_store,
        workflow_store=workflow_store,
        registry=registry,
        handlers=handlers,
    )
    return task_store, workflow_store, registry, coordinator


def step(
    step_id: str = "step_one",
    *,
    capability_id: str = "demo.transform",
    value: int = 3,
    depends_on: list[str] | None = None,
    requires_approval: bool = False,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        capability_id=capability_id,
        objective=f"Execute controlled operation for {step_id}.",
        input_payload={"value": value},
        depends_on=depends_on or [],
        requires_approval=requires_approval,
    )


def submit(coordinator, plan, identity=None):
    return coordinator.submit(
        "task_001",
        plan,
        principal=identity or principal(),
        occurred_at=150,
        authorization_now_epoch=150,
    )


def test_two_step_workflow_runs_in_dependency_order_and_persists(tmp_path):
    calls = []

    def transform(payload, dependencies, context: ExecutionContext):
        calls.append(context.step_id)
        if context.step_id == "step_one":
            return {"result": payload["value"] * 2}
        return {"result": dependencies["step_one"]["result"] + payload["value"]}

    task_store, workflow_store, registry, coordinator = stack(
        tmp_path, handlers={"demo.transform": transform}
    )
    plan = make_plan(
        [
            step("step_two", value=2, depends_on=["step_one"]),
            step("step_one", value=3),
        ]
    )
    initial = submit(coordinator, plan)
    assert initial.workflow_state == "ready"
    assert all(item.status == "pending" for item in initial.steps)
    assert calls == []

    outcome = coordinator.run_until_blocked(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert outcome.status == "succeeded"
    assert outcome.task_state == "succeeded"
    assert outcome.workflow_state == "succeeded"
    assert calls == ["step_one", "step_two"]

    final = workflow_store.get_workflow(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    by_id = {item.step_id: item for item in final.steps}
    assert by_id["step_one"].output_payload == {"result": 6}
    assert by_id["step_two"].output_payload == {"result": 8}
    assert all(item.status == "succeeded" for item in final.steps)
    assert task_store.verify_audit_chain(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    events = workflow_store.list_workflow_events(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert events[0].event_type == "workflow.created"
    assert events[-1].payload["workflow_state"] == "succeeded"


def test_submission_validates_plan_before_persisting_task(tmp_path):
    task_store, workflow_store, _, coordinator = stack(tmp_path)
    invalid = make_plan([
        PlanStep(
            step_id="step_one",
            capability_id="demo.transform",
            objective="Try invalid input.",
            input_payload={"wrong": "shape"},
        )
    ])
    with pytest.raises(ValueError, match="does not match"):
        submit(coordinator, invalid)
    assert task_store.get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    ) is None
    with pytest.raises(Exception):
        workflow_store.get_workflow(
            "task_001", principal=principal(), authorization_now_epoch=150
        )


def test_side_effect_step_waits_for_explicit_tenant_scoped_approval(tmp_path):
    calls = []

    def send(payload, dependencies, context):
        calls.append((context.step_id, context.idempotency_key))
        return {"result": payload["value"]}

    effect = capability(
        capability_id="demo.send",
        name="Send demo action",
        description="Send a demo action only after authorized approval.",
        required_permissions=["demo:send"],
        has_side_effects=True,
        approval_required=True,
    )
    _, workflow_store, _, coordinator = stack(
        tmp_path, handlers={"demo.send": send}, specs=[effect]
    )
    plan = make_plan(
        [step("step_one", capability_id="demo.send", requires_approval=True)]
    )
    submit(coordinator, plan)

    first = coordinator.run_until_blocked(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert first.status == "awaiting_approval"
    assert first.task_state == "awaiting_approval"
    assert first.awaiting_step_id == "step_one"
    assert calls == []

    wrong_tenant = principal(
        subject_id="reviewer_002",
        tenant_id="tenant_beta",
        permissions=frozenset({"workflow:approve", "workflow:read"}),
    )
    with pytest.raises(WorkflowNotFoundError):
        coordinator.approve_step(
            "task_001",
            "step_one",
            approver=wrong_tenant,
            occurred_at=151,
            authorization_now_epoch=150,
        )

    reviewer = principal(
        subject_id="reviewer_002",
        permissions=frozenset({"workflow:approve", "workflow:read"}),
    )
    approved = coordinator.approve_step(
        "task_001",
        "step_one",
        approver=reviewer,
        occurred_at=151,
        authorization_now_epoch=150,
    )
    assert approved.approved_by == "reviewer_002"
    assert approved.approval_fingerprint is not None
    assert approved.status == "pending"

    final = coordinator.run_until_blocked(
        "task_001", principal=principal(), authorization_now_epoch=152
    )
    assert final.status == "succeeded"
    assert len(calls) == 1
    assert len(calls[0][1]) == 64
    assert workflow_store.get_workflow(
        "task_001", principal=principal(), authorization_now_epoch=150
    ).steps[0].status == "succeeded"


def test_store_refuses_to_start_approval_required_step_without_approval(tmp_path):
    effect = capability(
        capability_id="demo.send",
        name="Send demo action",
        description="Send a demo action requiring approval.",
        required_permissions=["demo:send"],
        has_side_effects=True,
        approval_required=True,
    )
    task_store, workflow_store, _, coordinator = stack(tmp_path, specs=[effect])
    plan = make_plan([step("step_one", capability_id="demo.send", requires_approval=True)])
    submit(coordinator, plan)
    with pytest.raises(StepApprovalRequired):
        workflow_store.start_step(
            "task_001",
            "step_one",
            principal=principal(),
            occurred_at=151,
            authorization_now_epoch=150,
        )


def test_missing_handler_fails_closed_and_blocks_dependent_steps(tmp_path):
    task_store, workflow_store, _, coordinator = stack(tmp_path, handlers={})
    plan = make_plan(
        [
            step("step_one", value=3),
            step("step_two", value=4, depends_on=["step_one"]),
        ]
    )
    submit(coordinator, plan)
    outcome = coordinator.run_until_blocked(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert outcome.status == "failed"
    assert outcome.failed_step_id == "step_one"
    final = workflow_store.get_workflow(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert {item.step_id: item.status for item in final.steps} == {
        "step_one": "failed",
        "step_two": "blocked",
    }
    assert {item.step_id: item.error_code for item in final.steps}["step_one"] == "handler_not_registered"
    assert task_store.get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    ).state == TaskState.FAILED


def test_invalid_handler_output_is_not_accepted_as_success(tmp_path):
    def invalid_handler(payload, dependencies, context):
        return {"unexpected": "output"}

    _, workflow_store, _, coordinator = stack(
        tmp_path, handlers={"demo.transform": invalid_handler}
    )
    submit(coordinator, make_plan([step()]))
    result = coordinator.run_until_blocked(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert result.status == "failed"
    assert result.failed_step_id == "step_one"
    record = workflow_store.get_workflow(
        "task_001", principal=principal(), authorization_now_epoch=150
    ).steps[0]
    assert record.status == "failed"
    assert record.error_code == "output_validation_failed"
    assert record.output_payload is None


def test_handler_exception_details_are_not_persisted(tmp_path):
    keys = []

    def broken(payload, dependencies, context):
        keys.append(context.idempotency_key)
        raise RuntimeError("private customer secret")

    _, workflow_store, _, coordinator = stack(
        tmp_path, handlers={"demo.transform": broken}
    )
    submit(coordinator, make_plan([step()]))
    identity = principal()
    outcome = coordinator.run_until_blocked(
        "task_001", principal=identity, authorization_now_epoch=150
    )
    assert outcome.status == "failed"
    record = workflow_store.get_workflow(
        "task_001", principal=identity, authorization_now_epoch=150
    ).steps[0]
    assert record.error_code == "handler_error"
    assert "private customer secret" not in str(record.model_dump())
    assert coordinator.idempotency_store.get(
        keys[0], principal=identity, authorization_now_epoch=150
    ).state == "uncertain"


def test_interrupted_running_step_is_not_replayed_automatically(tmp_path):
    calls = []

    def handler(payload, dependencies, context):
        calls.append(context.step_id)
        return {"result": payload["value"]}

    task_store, workflow_store, _, coordinator = stack(
        tmp_path, handlers={"demo.transform": handler}
    )
    submitted = submit(coordinator, make_plan([step()]))
    task = task_store.apply_event(
        "task_001",
        TaskEvent.PLAN_ACCEPTED,
        principal=principal(),
        expected_version=submitted.task.version,
        occurred_at=150,
        authorization_now_epoch=150,
    )
    task_store.apply_event(
        "task_001",
        TaskEvent.START,
        principal=principal(),
        expected_version=task.version,
        occurred_at=150,
        authorization_now_epoch=150,
    )
    workflow_store.set_workflow_state(
        "task_001", "running", principal=principal(),
        occurred_at=150, authorization_now_epoch=150
    )
    workflow_store.start_step(
        "task_001", "step_one", principal=principal(),
        occurred_at=150, authorization_now_epoch=150
    )

    outcome = coordinator.run_until_blocked(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert outcome.status == "failed"
    assert calls == []
    record = workflow_store.get_workflow(
        "task_001", principal=principal(), authorization_now_epoch=150
    ).steps[0]
    assert record.status == "interrupted"
    assert record.error_code == "execution_interrupted"


def test_workflow_step_mutation_fails_when_audit_projection_is_tampered(tmp_path):
    task_store, workflow_store, _, coordinator = stack(tmp_path)
    submit(coordinator, make_plan([step()]))
    with sqlite3.connect(task_store.database_path) as connection:
        connection.execute(
            """
            UPDATE cmx_workflow_steps SET status = 'succeeded'
            WHERE tenant_id = ? AND task_id = ? AND step_id = ?
            """,
            ("tenant_alpha", "task_001", "step_one"),
        )
    with pytest.raises(WorkflowIntegrityError, match="failed integrity"):
        workflow_store.start_step(
            "task_001", "step_one", principal=principal(),
            occurred_at=151, authorization_now_epoch=150
        )


def test_schema_migration_is_idempotent_across_store_recreation(tmp_path):
    path = tmp_path / "cmx.sqlite3"
    task_store = SQLiteTaskStore(path)
    workflow_store = SQLiteWorkflowStore(task_store)
    workflow_store_again = SQLiteWorkflowStore(SQLiteTaskStore(path))
    assert workflow_store.database_path == workflow_store_again.database_path


def test_unverified_identity_cannot_submit_workflow(tmp_path):
    _, _, _, coordinator = stack(tmp_path)
    with pytest.raises(AuthorizationError) as captured:
        submit(
            coordinator,
            make_plan([step()]),
            identity=principal(verified=False),
        )
    assert captured.value.code == "unauthenticated"


def test_duplicate_workflow_plan_id_is_rejected_in_same_tenant(tmp_path):
    _, _, _, coordinator = stack(tmp_path)
    submit(coordinator, make_plan([step()], plan_id="plan_001"))
    with pytest.raises(ValueError):
        submit(coordinator, make_plan([step()], plan_id="plan_001"))


def test_capability_contract_drift_after_submission_is_rejected(tmp_path):
    calls = []

    def handler(payload, dependencies, context):
        calls.append(context.step_id)
        return {"result": payload["value"]}

    task_store, workflow_store, _, coordinator = stack(
        tmp_path, handlers={"demo.transform": handler}
    )
    submit(coordinator, make_plan([step()]))

    changed_registry = CapabilityRegistry()
    changed_registry.register(
        capability(
            version="1.0.1",
            output_schema={
                "type": "object",
                "properties": {"result": {"type": "number"}, "extra": {"type": "string"}},
                "required": ["result"],
                "additionalProperties": False,
            },
        )
    )
    coordinator.registry = changed_registry

    from cmx.workflow_store import WorkflowIntegrityError as PinError

    with pytest.raises(PinError, match="changed since workflow submission"):
        coordinator.run_until_blocked(
            "task_001", principal=principal(), authorization_now_epoch=150
        )
    assert calls == []
    assert task_store.get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    ).state == TaskState.CREATED


def test_capability_contract_drift_between_steps_stops_before_next_handler(tmp_path):
    calls = []
    holder = {}

    def handler(payload, dependencies, context):
        calls.append(context.step_id)
        if context.step_id == "step_one":
            holder["registry"].get("demo.transform").output_schema = {
                "type": "object",
                "properties": {
                    "result": {"type": "number"},
                    "extra": {"type": "string"},
                },
                "required": ["result"],
                "additionalProperties": False,
            }
        return {"result": payload["value"]}

    task_store, workflow_store, registry, coordinator = stack(
        tmp_path, handlers={"demo.transform": handler}
    )
    holder["registry"] = registry
    plan = make_plan(
        [
            step("step_one", value=1),
            step("step_two", value=2, depends_on=["step_one"]),
        ]
    )
    submit(coordinator, plan)

    from cmx.workflow_store import WorkflowIntegrityError as PinError

    with pytest.raises(PinError, match="changed since workflow submission"):
        coordinator.run_until_blocked(
            "task_001", principal=principal(), authorization_now_epoch=150
        )

    assert calls == ["step_one"]
    snapshot = workflow_store.get_workflow(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert {item.step_id: item.status for item in snapshot.steps} == {
        "step_one": "succeeded",
        "step_two": "pending",
    }
    assert task_store.get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    ).state == TaskState.RUNNING


def test_oversized_workflow_plan_is_rejected_before_task_creation(tmp_path):
    large_capability = capability(
        input_schema={
            "type": "object",
            "properties": {
                "value": {"type": "number"},
                "note": {"type": "string"},
            },
            "required": ["value", "note"],
            "additionalProperties": False,
        }
    )
    task_store, _, _, coordinator = stack(tmp_path, specs=[large_capability])
    large_text = "x" * 200_000
    steps = [
        PlanStep(
            step_id=f"step_{index}",
            capability_id="demo.transform",
            objective=f"Hold bounded payload {index}.",
            input_payload={"value": index, "note": large_text},
        )
        for index in range(1, 7)
    ]
    plan = make_plan(steps, plan_id="plan_large")
    with pytest.raises(ValueError, match="maximum encoded size"):
        submit(coordinator, plan)
    assert task_store.get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    ) is None


def test_existing_idempotency_reservation_blocks_handler_dispatch(tmp_path):
    calls = []

    def transform(payload, dependencies, context: ExecutionContext):
        calls.append(context.step_id)
        return {"result": payload["value"] * 2}

    _, workflow_store, registry, coordinator = stack(
        tmp_path, handlers={"demo.transform": transform}
    )
    identity = principal()
    plan = make_plan([step()])
    submitted = submit(coordinator, plan, identity)
    capability_spec = registry.get("demo.transform")
    key = hashlib.sha256(
        _stable_key(
            identity.tenant_id, "task_001", plan.plan_id, "step_one",
            capability_spec.version, {"value": 3},
        ).encode("utf-8")
    ).hexdigest()
    request = {
        "tenant_id": identity.tenant_id,
        "task_id": "task_001",
        "plan_id": plan.plan_id,
        "plan_hash": submitted.plan_hash,
        "step_id": "step_one",
        "capability_id": capability_spec.capability_id,
        "capability_version": capability_spec.version,
        "capability_fingerprint": _capability_fingerprint(capability_spec),
        "input_payload": {"value": 3},
        "dependency_outputs": {},
    }
    coordinator.idempotency_store.reserve(
        "task_001:step_one", key, request, principal=identity,
        occurred_at=150, authorization_now_epoch=150,
    )

    outcome = coordinator.run_until_blocked(
        "task_001", principal=identity, authorization_now_epoch=150,
    )
    final = workflow_store.get_workflow(
        "task_001", principal=identity, authorization_now_epoch=151,
    )
    assert calls == []
    assert final.steps[0].status == "interrupted"
    assert final.task.state == TaskState.FAILED
    assert outcome.task_state == TaskState.FAILED
    assert outcome.workflow_state == "failed"
    assert "replay was blocked" in outcome.message.lower()
