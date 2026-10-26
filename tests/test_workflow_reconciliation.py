import pytest

from cmx.workflow_reconciliation import SQLiteWorkflowReconciler
from cmx.workflow_store import WorkflowConflictError, WorkflowNotFoundError
from cmx.security import AuthorizationError
from cmx.worker_leases import SQLiteWorkerLeaseStore
from cmx.kernel import CapabilityRegistry, CapabilitySpec
from test_worker_leases import principal, ready_workflow


def reviewer():
    identity = principal()
    return identity.model_copy(update={
        "permissions": identity.permissions | frozenset({"workflow:reconcile", "audit:write", "audit:read"})
    })


def interrupted_store(tmp_path):
    task_store, store = ready_workflow(tmp_path)
    identity = reviewer()
    leases = SQLiteWorkerLeaseStore(task_store)
    lease = leases.claim(
        "task_001", "step_one", worker_id="worker_recovered",
        lease_seconds=5, principal=identity, now_epoch=200, authorization_now_epoch=200,
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
    store, identity = interrupted_store(tmp_path)
    # Simulate a lease whose expiry is still in the future after an interrupted step
    # was persisted; this tests the lease guard independently of interrupt-step policy.
    with store.task_store._connection() as connection:
        connection.execute(
            "UPDATE cmx_worker_leases SET expires_at = 300 WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
            (identity.tenant_id, "task_001", "step_one"),
        )
    with pytest.raises(WorkflowConflictError, match="live worker lease"):
        SQLiteWorkflowReconciler(store).record_review(
            "task_001", "step_one", decision="inconclusive",
            evidence_reference="ticket:CMX-3", notes="Attempted with a future audit timestamp.",
            principal=identity, occurred_at=400, authorization_now_epoch=210,
        )



def test_inventory_exposes_uncertain_step_and_latest_review_without_mutation(tmp_path):
    store, identity = interrupted_store(tmp_path)
    reconciler = SQLiteWorkflowReconciler(store)
    before = store.get_workflow("task_001", principal=identity, authorization_now_epoch=210)
    initial = reconciler.inspect_uncertain_steps(
        "task_001", principal=identity, authorization_now_epoch=210,
    )
    assert len(initial) == 1
    assert initial[0]["step_id"] == "step_one"
    assert initial[0]["step_status"] == "interrupted"
    assert initial[0]["lease_state"] == "expired"
    assert initial[0]["actionable"] is True
    assert initial[0]["latest_review"] is None

    reconciler.record_review(
        "task_001", "step_one", decision="inconclusive",
        evidence_reference="ticket:CMX-4", notes="Provider outcome still being investigated.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    reviewed = reconciler.inspect_uncertain_steps(
        "task_001", principal=identity, authorization_now_epoch=210,
    )
    after = store.get_workflow("task_001", principal=identity, authorization_now_epoch=210)
    assert reviewed[0]["latest_review"]["decision"] == "inconclusive"
    assert reviewed[0]["latest_review"]["evidence_reference"] == "ticket:CMX-4"
    assert before.steps[0].status == after.steps[0].status == "interrupted"
    assert before.workflow_state == after.workflow_state


def test_inventory_marks_interrupted_step_non_actionable_with_live_lease(tmp_path):
    store, identity = interrupted_store(tmp_path)
    with store.task_store._connection() as connection:
        connection.execute(
            "UPDATE cmx_worker_leases SET expires_at = 300 WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
            (identity.tenant_id, "task_001", "step_one"),
        )
    result = SQLiteWorkflowReconciler(store).inspect_uncertain_steps(
        "task_001", principal=identity, authorization_now_epoch=210,
    )
    assert result[0]["lease_state"] == "active"
    assert result[0]["actionable"] is False



def test_success_resolution_preflight_validates_output_without_mutation(tmp_path):
    store, identity = interrupted_store(tmp_path)
    reconciler = SQLiteWorkflowReconciler(store)
    reconciler.record_review(
        "task_001", "step_one", decision="effect_confirmed",
        evidence_reference="ledger:TX-88", notes="External ledger confirms the operation.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform demo value",
        description="Controlled worker lease test.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=10, max_retries=0,
    ))
    before = store.get_workflow("task_001", principal=identity, authorization_now_epoch=211)
    candidate = reconciler.validate_success_resolution(
        "task_001", "step_one", output_payload={"result": 5},
        registry=registry, principal=identity, authorization_now_epoch=211,
    )
    after = store.get_workflow("task_001", principal=identity, authorization_now_epoch=211)
    assert candidate["validated"] is True
    assert candidate["committed"] is False
    assert candidate["output_payload"] == {"result": 5}
    assert before.steps[0].status == after.steps[0].status == "interrupted"
    assert before.steps[0].output_payload == after.steps[0].output_payload is None


def test_success_resolution_preflight_rejects_invalid_output(tmp_path):
    store, identity = interrupted_store(tmp_path)
    reconciler = SQLiteWorkflowReconciler(store)
    reconciler.record_review(
        "task_001", "step_one", decision="effect_confirmed",
        evidence_reference="ledger:TX-89", notes="External effect verified.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform demo value",
        description="Controlled worker lease test.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=10, max_retries=0,
    ))
    from cmx.schema_validation import CapabilityPayloadValidationError
    with pytest.raises(CapabilityPayloadValidationError):
        reconciler.validate_success_resolution(
            "task_001", "step_one", output_payload={"unexpected": "shape"},
            registry=registry, principal=identity, authorization_now_epoch=211,
        )


def test_success_resolution_preflight_requires_latest_review_to_confirm_effect(tmp_path):
    store, identity = interrupted_store(tmp_path)
    reconciler = SQLiteWorkflowReconciler(store)
    reconciler.record_review(
        "task_001", "step_one", decision="effect_confirmed",
        evidence_reference="ledger:TX-90", notes="Initially confirmed.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    reconciler.record_review(
        "task_001", "step_one", decision="inconclusive",
        evidence_reference="incident:INC-90", notes="Subsequent review reopened uncertainty.",
        principal=identity, occurred_at=211, authorization_now_epoch=211,
    )
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform demo value",
        description="Controlled worker lease test.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=10, max_retries=0,
    ))
    with pytest.raises(WorkflowConflictError, match="latest reconciliation review"):
        reconciler.validate_success_resolution(
            "task_001", "step_one", output_payload={"result": 5},
            registry=registry, principal=identity, authorization_now_epoch=212,
        )



def test_retry_candidate_rejects_exhausted_attempt_budget(tmp_path):
    store, identity = interrupted_store(tmp_path)
    reconciler = SQLiteWorkflowReconciler(store)
    reconciler.record_review(
        "task_001", "step_one", decision="effect_not_applied",
        evidence_reference="provider:lookup-101", notes="Provider confirms no operation was applied.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform demo value",
        description="Controlled worker lease test.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=10, max_retries=0,
    ))
    with pytest.raises(WorkflowConflictError, match="retry limit"):
        reconciler.validate_retry_candidate(
            "task_001", "step_one", registry=registry,
            principal=identity, authorization_now_epoch=211,
        )


def test_retry_candidate_does_not_treat_confirmed_effect_as_retryable(tmp_path):
    store, identity = interrupted_store(tmp_path)
    reconciler = SQLiteWorkflowReconciler(store)
    reconciler.record_review(
        "task_001", "step_one", decision="effect_confirmed",
        evidence_reference="provider:lookup-102", notes="Provider confirms operation completed.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform demo value",
        description="Controlled worker lease test.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=10, max_retries=0,
    ))
    with pytest.raises(WorkflowConflictError, match="effect was not applied"):
        reconciler.validate_retry_candidate(
            "task_001", "step_one", registry=registry,
            principal=identity, authorization_now_epoch=211,
        )



def test_retry_candidate_is_read_only_when_all_preconditions_hold(tmp_path):
    from cmx.orchestration import ExecutionCoordinator
    from cmx.kernel import ExecutionPlan, PlanStep
    from cmx.persistence import SQLiteTaskStore
    from cmx.workflow_store import SQLiteWorkflowStore

    task_store = SQLiteTaskStore(tmp_path / "retryable.sqlite3")
    store = SQLiteWorkflowStore(task_store)
    identity = reviewer()
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform demo value",
        description="Controlled worker lease test.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=10, max_retries=1,
    ))
    coordinator = ExecutionCoordinator(
        task_store=task_store, workflow_store=store, registry=registry, handlers={},
    )
    plan = ExecutionPlan(
        plan_id="plan_retry", objective="Test retry-candidate preflight.",
        steps=[PlanStep(
            step_id="step_retry", capability_id="demo.transform",
            objective="Exercise a retryable interrupted operation.",
            input_payload={"value": 3}, max_attempts=2,
        )],
    )
    coordinator.submit(
        "task_retry", plan, principal=identity, occurred_at=150, authorization_now_epoch=150,
    )
    lease = SQLiteWorkerLeaseStore(task_store).claim(
        "task_retry", "step_retry", worker_id="worker_retry", lease_seconds=5,
        principal=identity, now_epoch=200, authorization_now_epoch=200,
    )
    store.start_step(
        "task_retry", "step_retry", principal=identity, occurred_at=201,
        authorization_now_epoch=201, worker_id=lease.worker_id, fencing_token=lease.fencing_token,
    )
    store.interrupt_step(
        "task_retry", "step_retry", principal=identity, occurred_at=206,
        authorization_now_epoch=206,
    )
    reconciler = SQLiteWorkflowReconciler(store)
    reconciler.record_review(
        "task_retry", "step_retry", decision="effect_not_applied",
        evidence_reference="provider:lookup-103",
        notes="Independent provider query confirms no side effect was applied.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    before = store.get_workflow("task_retry", principal=identity, authorization_now_epoch=211)
    result = reconciler.validate_retry_candidate(
        "task_retry", "step_retry", registry=registry,
        principal=identity, authorization_now_epoch=211,
    )
    after = store.get_workflow("task_retry", principal=identity, authorization_now_epoch=211)
    assert result["candidate"] is True
    assert result["retry_authorized"] is False
    assert result["automatic_replay"] is False
    assert before.steps[0].status == after.steps[0].status == "interrupted"
    assert before.steps[0].attempts == after.steps[0].attempts == 1



def test_atomic_non_application_resolution_marks_step_failed_and_audits_once(tmp_path):
    store, identity = interrupted_store(tmp_path)
    reconciler = SQLiteWorkflowReconciler(store)
    reconciler.record_review(
        "task_001", "step_one", decision="effect_not_applied",
        evidence_reference="provider:lookup-atomic-1",
        notes="Independent provider evidence confirms no side effect was applied.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    before_events = store.list_workflow_events(
        "task_001", principal=identity, authorization_now_epoch=211,
    )
    result = reconciler.resolve_confirmed_non_application(
        "task_001", "step_one", principal=identity,
        occurred_at=212, authorization_now_epoch=212,
    )
    after = store.get_workflow("task_001", principal=identity, authorization_now_epoch=212)
    after_events = store.list_workflow_events(
        "task_001", principal=identity, authorization_now_epoch=212,
    )
    assert result["committed"] is True
    assert result["automatic_replay"] is False
    assert result["resolution"] == "effect_not_applied"
    assert after.steps[0].status == "failed"
    assert after.workflow_state == "failed"
    assert after.task.state.value == "failed"
    assert store.task_store.verify_audit_chain(
        "task_001", principal=identity, authorization_now_epoch=212,
    ) is True
    assert len(after_events) == len(before_events) + 1
    assert after_events[-1].event_type == "step.reconciliation_resolved_failed"
    assert after_events[-1].payload["details"]["review_event_id"] == result["review_event_id"]
    assert after_events[-1].payload["details"]["automatic_replay"] is False


def test_atomic_non_application_resolution_requires_latest_review(tmp_path):
    store, identity = interrupted_store(tmp_path)
    reconciler = SQLiteWorkflowReconciler(store)
    reconciler.record_review(
        "task_001", "step_one", decision="effect_not_applied",
        evidence_reference="provider:lookup-atomic-2",
        notes="Initial review confirms no side effect.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    reconciler.record_review(
        "task_001", "step_one", decision="inconclusive",
        evidence_reference="incident:atomic-2",
        notes="A later review reopened uncertainty.",
        principal=identity, occurred_at=211, authorization_now_epoch=211,
    )
    before = store.get_workflow("task_001", principal=identity, authorization_now_epoch=212)
    with pytest.raises(WorkflowConflictError, match="latest reconciliation review"):
        reconciler.resolve_confirmed_non_application(
            "task_001", "step_one", principal=identity,
            occurred_at=212, authorization_now_epoch=212,
        )
    after = store.get_workflow("task_001", principal=identity, authorization_now_epoch=212)
    assert before.steps[0].status == after.steps[0].status == "interrupted"

def test_atomic_success_resolution_commits_output_and_audit_without_resuming(tmp_path):
    store, identity = interrupted_store(tmp_path)
    reconciler = SQLiteWorkflowReconciler(store)
    reconciler.record_review(
        "task_001", "step_one", decision="effect_confirmed",
        evidence_reference="provider:ledger-success-1",
        notes="Provider ledger confirms the operation completed.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform demo value",
        description="Controlled worker lease test.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=10, max_retries=0,
    ))
    before_events = store.list_workflow_events(
        "task_001", principal=identity, authorization_now_epoch=211,
    )
    result = reconciler.resolve_confirmed_success(
        "task_001", "step_one", output_payload={"result": 42},
        registry=registry, principal=identity, occurred_at=212, authorization_now_epoch=212,
    )
    after = store.get_workflow("task_001", principal=identity, authorization_now_epoch=212)
    after_events = store.list_workflow_events(
        "task_001", principal=identity, authorization_now_epoch=212,
    )
    assert result["committed"] is True
    assert result["automatic_replay"] is False
    assert result["workflow_resumed"] is False
    assert result["status"] == "succeeded"
    assert after.steps[0].status == "succeeded"
    assert after.steps[0].output_payload == {"result": 42}
    assert after.workflow_state == "failed"
    assert after.task.state.value == "failed"
    assert store.task_store.verify_audit_chain(
        "task_001", principal=identity, authorization_now_epoch=212,
    ) is True
    assert len(after_events) == len(before_events) + 1
    assert after_events[-1].event_type == "step.reconciliation_resolved_succeeded"
    assert after_events[-1].payload["details"]["review_event_id"] == result["review_event_id"]
    assert after_events[-1].payload["details"]["workflow_resumed"] is False


def test_atomic_success_resolution_rejects_invalid_output_without_mutation(tmp_path):
    store, identity = interrupted_store(tmp_path)
    reconciler = SQLiteWorkflowReconciler(store)
    reconciler.record_review(
        "task_001", "step_one", decision="effect_confirmed",
        evidence_reference="provider:ledger-success-2",
        notes="Provider ledger confirms the operation completed.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform demo value",
        description="Controlled worker lease test.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=10, max_retries=0,
    ))
    before = store.get_workflow("task_001", principal=identity, authorization_now_epoch=211)
    before_events = store.list_workflow_events(
        "task_001", principal=identity, authorization_now_epoch=211,
    )
    with pytest.raises(ValueError):
        reconciler.resolve_confirmed_success(
            "task_001", "step_one", output_payload={"unexpected": True},
            registry=registry, principal=identity, occurred_at=212, authorization_now_epoch=212,
        )
    after = store.get_workflow("task_001", principal=identity, authorization_now_epoch=212)
    after_events = store.list_workflow_events(
        "task_001", principal=identity, authorization_now_epoch=212,
    )
    assert after.steps[0].status == before.steps[0].status == "interrupted"
    assert after.workflow_state == before.workflow_state == "failed"
    assert len(after_events) == len(before_events)



def _retry_authorization_fixture(tmp_path):
    from cmx.orchestration import ExecutionCoordinator
    from cmx.kernel import ExecutionPlan, PlanStep
    from cmx.persistence import SQLiteTaskStore
    from cmx.workflow_store import SQLiteWorkflowStore

    task_store = SQLiteTaskStore(tmp_path / "retry-authorization.sqlite3")
    store = SQLiteWorkflowStore(task_store)
    identity = reviewer()
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform demo value",
        description="Controlled retry authorization test.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=10, max_retries=1,
    ))
    coordinator = ExecutionCoordinator(
        task_store=task_store, workflow_store=store, registry=registry, handlers={},
    )
    plan = ExecutionPlan(
        plan_id="plan_retry_auth", objective="Test durable retry authorization.",
        steps=[PlanStep(
            step_id="step_retry_auth", capability_id="demo.transform",
            objective="Exercise an interrupted operation without replay.",
            input_payload={"value": 3}, max_attempts=2,
        )],
    )
    coordinator.submit(
        "task_retry_auth", plan, principal=identity,
        occurred_at=150, authorization_now_epoch=150,
    )
    lease = SQLiteWorkerLeaseStore(task_store).claim(
        "task_retry_auth", "step_retry_auth", worker_id="worker_retry_auth",
        lease_seconds=5, principal=identity, now_epoch=200, authorization_now_epoch=200,
    )
    store.start_step(
        "task_retry_auth", "step_retry_auth", principal=identity,
        occurred_at=201, authorization_now_epoch=201,
        worker_id=lease.worker_id, fencing_token=lease.fencing_token,
    )
    store.interrupt_step(
        "task_retry_auth", "step_retry_auth", principal=identity,
        occurred_at=206, authorization_now_epoch=206,
    )
    reconciler = SQLiteWorkflowReconciler(store)
    reconciler.record_review(
        "task_retry_auth", "step_retry_auth", decision="effect_not_applied",
        evidence_reference="provider:lookup-retry-auth",
        notes="Independent provider lookup confirms the operation was not applied.",
        principal=identity, occurred_at=210, authorization_now_epoch=210,
    )
    return task_store, store, identity, registry, reconciler


def test_retry_authorization_is_durable_audited_and_does_not_start_retry(tmp_path):
    task_store, store, identity, registry, reconciler = _retry_authorization_fixture(tmp_path)
    before = store.get_workflow(
        "task_retry_auth", principal=identity, authorization_now_epoch=211,
    )
    result = reconciler.record_retry_authorization(
        "task_retry_auth", "step_retry_auth", registry=registry,
        principal=identity, occurred_at=211, authorization_now_epoch=211,
    )
    after = store.get_workflow(
        "task_retry_auth", principal=identity, authorization_now_epoch=211,
    )
    events = store.list_workflow_events(
        "task_retry_auth", principal=identity, authorization_now_epoch=211,
    )
    event = events[-1]
    assert result["authorization_recorded"] is True
    assert result["authorization_id"]
    assert result["retry_started"] is False
    assert result["automatic_replay"] is False
    assert result["single_use_enforced"] is False
    assert event.event_type == "step.retry_authorization_recorded"
    assert event.payload["details"]["authorization_id"] == result["authorization_id"]
    assert event.payload["details"]["review_event_id"] == result["review_event_id"]
    assert event.payload["details"]["automatic_replay"] is False
    assert event.payload["details"]["retry_started"] is False
    assert before.steps[0].status == after.steps[0].status == "interrupted"
    assert before.steps[0].attempts == after.steps[0].attempts == 1


def test_retry_authorization_rejects_duplicate_record_for_same_step(tmp_path):
    _, store, identity, registry, reconciler = _retry_authorization_fixture(tmp_path)
    reconciler.record_retry_authorization(
        "task_retry_auth", "step_retry_auth", registry=registry,
        principal=identity, occurred_at=211, authorization_now_epoch=211,
    )
    with pytest.raises(WorkflowConflictError, match="already exists"):
        reconciler.record_retry_authorization(
            "task_retry_auth", "step_retry_auth", registry=registry,
            principal=identity, occurred_at=212, authorization_now_epoch=212,
        )


def test_retry_authorization_rejects_when_latest_review_is_inconclusive(tmp_path):
    _, store, identity, registry, reconciler = _retry_authorization_fixture(tmp_path)
    reconciler.record_review(
        "task_retry_auth", "step_retry_auth", decision="inconclusive",
        evidence_reference="provider:follow-up",
        notes="The latest check cannot establish the operation outcome.",
        principal=identity, occurred_at=212, authorization_now_epoch=212,
    )
    with pytest.raises(WorkflowConflictError, match="effect was not applied"):
        reconciler.record_retry_authorization(
            "task_retry_auth", "step_retry_auth", registry=registry,
            principal=identity, occurred_at=213, authorization_now_epoch=213,
        )


def test_retry_authorization_audit_failure_rolls_back_record(tmp_path, monkeypatch):
    _, store, identity, registry, reconciler = _retry_authorization_fixture(tmp_path)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("simulated workflow audit failure")

    monkeypatch.setattr(store, "_record_step_event", fail_audit)
    with pytest.raises(RuntimeError, match="simulated workflow audit failure"):
        reconciler.record_retry_authorization(
            "task_retry_auth", "step_retry_auth", registry=registry,
            principal=identity, occurred_at=211, authorization_now_epoch=211,
        )
    monkeypatch.undo()
    events = store.list_workflow_events(
        "task_retry_auth", principal=identity, authorization_now_epoch=212,
    )
    assert not any(event.event_type == "step.retry_authorization_recorded" for event in events)



def test_retry_authorization_consumption_is_single_use_and_never_starts_retry(tmp_path):
    _, store, identity, registry, reconciler = _retry_authorization_fixture(tmp_path)
    authorization = reconciler.record_retry_authorization(
        "task_retry_auth", "step_retry_auth", registry=registry,
        principal=identity, occurred_at=211, authorization_now_epoch=211,
    )
    consumed = reconciler.consume_retry_authorization(
        "task_retry_auth", "step_retry_auth", authorization["authorization_id"],
        registry=registry, principal=identity, occurred_at=212, authorization_now_epoch=212,
    )
    assert consumed["authorization_consumed"] is True
    assert consumed["retry_started"] is False
    assert consumed["automatic_replay"] is False
    events = store.list_workflow_events(
        "task_retry_auth", principal=identity, authorization_now_epoch=212,
    )
    event = events[-1]
    assert event.event_type == "step.retry_authorization_consumed"
    assert event.payload["details"]["authorization_id"] == authorization["authorization_id"]
    assert event.payload["details"]["retry_started"] is False
    workflow = store.get_workflow(
        "task_retry_auth", principal=identity, authorization_now_epoch=212,
    )
    assert workflow.steps[0].status == "interrupted"
    assert workflow.steps[0].attempts == 1


def test_retry_authorization_cannot_be_consumed_twice(tmp_path):
    _, _, identity, registry, reconciler = _retry_authorization_fixture(tmp_path)
    authorization = reconciler.record_retry_authorization(
        "task_retry_auth", "step_retry_auth", registry=registry,
        principal=identity, occurred_at=211, authorization_now_epoch=211,
    )
    reconciler.consume_retry_authorization(
        "task_retry_auth", "step_retry_auth", authorization["authorization_id"],
        registry=registry, principal=identity, occurred_at=212, authorization_now_epoch=212,
    )
    with pytest.raises(WorkflowConflictError, match="already been consumed"):
        reconciler.consume_retry_authorization(
            "task_retry_auth", "step_retry_auth", authorization["authorization_id"],
            registry=registry, principal=identity, occurred_at=213, authorization_now_epoch=213,
        )


def test_retry_authorization_cannot_be_consumed_after_review_changes(tmp_path):
    _, _, identity, registry, reconciler = _retry_authorization_fixture(tmp_path)
    authorization = reconciler.record_retry_authorization(
        "task_retry_auth", "step_retry_auth", registry=registry,
        principal=identity, occurred_at=211, authorization_now_epoch=211,
    )
    reconciler.record_review(
        "task_retry_auth", "step_retry_auth", decision="inconclusive",
        evidence_reference="provider:review-changed",
        notes="New evidence makes the prior decision inconclusive.",
        principal=identity, occurred_at=212, authorization_now_epoch=212,
    )
    with pytest.raises(WorkflowConflictError, match="review changed"):
        reconciler.consume_retry_authorization(
            "task_retry_auth", "step_retry_auth", authorization["authorization_id"],
            registry=registry, principal=identity, occurred_at=213, authorization_now_epoch=213,
        )


def test_retry_authorization_consumption_rolls_back_when_audit_write_fails(tmp_path, monkeypatch):
    _, store, identity, registry, reconciler = _retry_authorization_fixture(tmp_path)
    authorization = reconciler.record_retry_authorization(
        "task_retry_auth", "step_retry_auth", registry=registry,
        principal=identity, occurred_at=211, authorization_now_epoch=211,
    )

    def fail_audit(*args, **kwargs):
        raise RuntimeError("simulated consumption audit failure")

    monkeypatch.setattr(store, "_record_step_event", fail_audit)
    with pytest.raises(RuntimeError, match="simulated consumption audit failure"):
        reconciler.consume_retry_authorization(
            "task_retry_auth", "step_retry_auth", authorization["authorization_id"],
            registry=registry, principal=identity, occurred_at=212, authorization_now_epoch=212,
        )
    monkeypatch.undo()
    events = store.list_workflow_events(
        "task_retry_auth", principal=identity, authorization_now_epoch=213,
    )
    assert not any(
        event.event_type == "step.retry_authorization_consumed"
        and event.payload.get("details", {}).get("authorization_id") == authorization["authorization_id"]
        for event in events
    )



def test_retry_authorization_consumption_is_tenant_scoped(tmp_path):
    _, store, owner, registry, reconciler = _retry_authorization_fixture(tmp_path)
    authorization = reconciler.record_retry_authorization(
        "task_retry_auth", "step_retry_auth", registry=registry,
        principal=owner, occurred_at=211, authorization_now_epoch=211,
    )
    other_tenant = owner.model_copy(update={"tenant_id": "tenant_other"})
    with pytest.raises(WorkflowNotFoundError):
        reconciler.consume_retry_authorization(
            "task_retry_auth", "step_retry_auth", authorization["authorization_id"],
            registry=registry, principal=other_tenant,
            occurred_at=212, authorization_now_epoch=212,
        )
    events = store.list_workflow_events(
        "task_retry_auth", principal=owner, authorization_now_epoch=213,
    )
    assert not any(
        event.event_type == "step.retry_authorization_consumed"
        for event in events
    )


def test_retry_authorization_consumption_requires_reconciliation_permission(tmp_path):
    _, _, owner, registry, reconciler = _retry_authorization_fixture(tmp_path)
    authorization = reconciler.record_retry_authorization(
        "task_retry_auth", "step_retry_auth", registry=registry,
        principal=owner, occurred_at=211, authorization_now_epoch=211,
    )
    unauthorized = owner.model_copy(update={
        "permissions": frozenset(
            permission for permission in owner.permissions
            if permission != "workflow:reconcile"
        )
    })
    with pytest.raises(AuthorizationError):
        reconciler.consume_retry_authorization(
            "task_retry_auth", "step_retry_auth", authorization["authorization_id"],
            registry=registry, principal=unauthorized,
            occurred_at=212, authorization_now_epoch=212,
        )
