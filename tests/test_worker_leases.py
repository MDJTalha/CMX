import pytest

from cmx.kernel import CapabilityRegistry, CapabilitySpec, ExecutionPlan, PlanStep
from cmx.orchestration import ExecutionCoordinator
from cmx.persistence import SQLiteTaskStore
from cmx.security import AuthorizationError, PrincipalContext
from cmx.worker_leases import SQLiteWorkerLeaseStore, WorkerLeaseConflict, WorkerLeaseNotFound
from cmx.workflow_store import SQLiteWorkflowStore


def principal(*, tenant_id="tenant_alpha"):
    return PrincipalContext(
        subject_id="user_001", tenant_id=tenant_id, issuer="https://identity.cmx.test",
        permissions=frozenset({"workflow:submit", "workflow:execute", "workflow:read",
                               "workflow:approve", "tasks:create", "tasks:read", "tasks:write",
                               "demo:execute"}),
        issued_at=100, expires_at=1000, authentication_verified=True,
        authentication_method="test", credential_id="cred_user_001",
    )


def ready_workflow(tmp_path):
    task_store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    workflow_store = SQLiteWorkflowStore(task_store)
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
    coordinator = ExecutionCoordinator(
        task_store=task_store, workflow_store=workflow_store, registry=registry, handlers={}
    )
    plan = ExecutionPlan(
        plan_id="plan_001", objective="Test durable worker reservation.",
        steps=[PlanStep(step_id="step_one", capability_id="demo.transform",
                        objective="Reserve one pending step.", input_payload={"value": 3})],
    )
    coordinator.submit("task_001", plan, principal=principal(), occurred_at=150,
                       authorization_now_epoch=150)
    return task_store, workflow_store


def test_claim_is_persisted_and_blocks_second_worker_until_expiry(tmp_path):
    task_store, _ = ready_workflow(tmp_path)
    leases = SQLiteWorkerLeaseStore(task_store)
    first = leases.claim(
        "task_001", "step_one", worker_id="worker_a", lease_seconds=10,
        principal=principal(), now_epoch=200, authorization_now_epoch=200,
    )
    assert first.fencing_token == 1
    assert first.expires_at == 210
    with pytest.raises(WorkerLeaseConflict):
        leases.claim(
            "task_001", "step_one", worker_id="worker_b", lease_seconds=10,
            principal=principal(), now_epoch=209, authorization_now_epoch=209,
        )


def test_expired_lease_reclaim_increments_fencing_token_and_fences_old_owner(tmp_path):
    task_store, _ = ready_workflow(tmp_path)
    leases = SQLiteWorkerLeaseStore(task_store)
    old = leases.claim(
        "task_001", "step_one", worker_id="worker_a", lease_seconds=5,
        principal=principal(), now_epoch=200, authorization_now_epoch=200,
    )
    new = leases.claim(
        "task_001", "step_one", worker_id="worker_b", lease_seconds=5,
        principal=principal(), now_epoch=205, authorization_now_epoch=205,
    )
    assert new.fencing_token == old.fencing_token + 1
    with pytest.raises(WorkerLeaseConflict):
        leases.renew(old, lease_seconds=10, principal=principal(), now_epoch=205, authorization_now_epoch=205)
    with pytest.raises(WorkerLeaseConflict):
        leases.release(old, principal=principal(), now_epoch=205, authorization_now_epoch=205)


def test_lease_renewal_and_release_require_current_fencing_token(tmp_path):
    task_store, _ = ready_workflow(tmp_path)
    leases = SQLiteWorkerLeaseStore(task_store)
    lease = leases.claim(
        "task_001", "step_one", worker_id="worker_a", lease_seconds=5,
        principal=principal(), now_epoch=200, authorization_now_epoch=200,
    )
    renewed = leases.renew(
        lease, lease_seconds=20, principal=principal(), now_epoch=202, authorization_now_epoch=202,
    )
    assert renewed.expires_at == 222
    leases.release(renewed, principal=principal(), now_epoch=203, authorization_now_epoch=203)
    with pytest.raises(WorkerLeaseConflict):
        leases.renew(renewed, lease_seconds=5, principal=principal(), now_epoch=204, authorization_now_epoch=204)


def test_claim_is_tenant_scoped_and_rejects_non_pending_step(tmp_path):
    task_store, workflow_store = ready_workflow(tmp_path)
    leases = SQLiteWorkerLeaseStore(task_store)
    with pytest.raises(WorkerLeaseNotFound):
        leases.claim(
            "task_001", "step_one", worker_id="worker_a", lease_seconds=5,
            principal=principal(tenant_id="tenant_other"), now_epoch=200, authorization_now_epoch=200,
        )
    workflow_store.start_step(
        "task_001", "step_one", principal=principal(), occurred_at=201, authorization_now_epoch=201,
    )
    with pytest.raises(WorkerLeaseConflict):
        leases.claim(
            "task_001", "step_one", worker_id="worker_a", lease_seconds=5,
            principal=principal(), now_epoch=202, authorization_now_epoch=202,
        )



def test_step_transitions_require_live_matching_lease_when_worker_fields_supplied(tmp_path):
    task_store, workflow_store = ready_workflow(tmp_path)
    leases = SQLiteWorkerLeaseStore(task_store)
    lease = leases.claim(
        "task_001", "step_one", worker_id="worker_a", lease_seconds=5,
        principal=principal(), now_epoch=200, authorization_now_epoch=200,
    )
    started = workflow_store.start_step(
        "task_001", "step_one", principal=principal(), occurred_at=202,
        authorization_now_epoch=202, worker_id=lease.worker_id,
        fencing_token=lease.fencing_token,
    )
    assert started.status == "running"
    with pytest.raises(Exception, match="lease is expired"):
        workflow_store.complete_step(
            "task_001", "step_one", {"result": 3}, principal=principal(),
            occurred_at=205, authorization_now_epoch=205,
            worker_id=lease.worker_id, fencing_token=lease.fencing_token,
        )
    unchanged = workflow_store.get_workflow(
        "task_001", principal=principal(), authorization_now_epoch=205,
    )
    assert unchanged.steps[0].status == "running"


def test_step_transition_rejects_a_superseded_fencing_token(tmp_path):
    task_store, workflow_store = ready_workflow(tmp_path)
    leases = SQLiteWorkerLeaseStore(task_store)
    lease = leases.claim(
        "task_001", "step_one", worker_id="worker_a", lease_seconds=30,
        principal=principal(), now_epoch=200, authorization_now_epoch=200,
    )
    workflow_store.start_step(
        "task_001", "step_one", principal=principal(), occurred_at=201,
        authorization_now_epoch=201, worker_id=lease.worker_id,
        fencing_token=lease.fencing_token,
    )
    with task_store._connection() as connection:
        connection.execute(
            """UPDATE cmx_worker_leases SET fencing_token=?
               WHERE tenant_id=? AND task_id=? AND step_id=?""",
            (lease.fencing_token + 1, lease.tenant_id, lease.task_id, lease.step_id),
        )
    with pytest.raises(Exception, match="lease is expired"):
        workflow_store.fail_step(
            "task_001", "step_one", error_code="handler_error", principal=principal(),
            occurred_at=202, authorization_now_epoch=202,
            worker_id=lease.worker_id, fencing_token=lease.fencing_token,
        )
    unchanged = workflow_store.get_workflow(
        "task_001", principal=principal(), authorization_now_epoch=202,
    )
    assert unchanged.steps[0].status == "running"


def test_active_lease_prevents_coordinator_from_interrupting_live_step(tmp_path):
    task_store, workflow_store = ready_workflow(tmp_path)
    leases = SQLiteWorkerLeaseStore(task_store)
    lease = leases.claim(
        "task_001", "step_one", worker_id="worker_live", lease_seconds=100,
        principal=principal(), now_epoch=200, authorization_now_epoch=200,
    )
    workflow_store.start_step(
        "task_001", "step_one", principal=principal(), occurred_at=201,
        authorization_now_epoch=201, worker_id=lease.worker_id,
        fencing_token=lease.fencing_token,
    )
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform demo value",
        description="Live lease coordination regression test.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=10, max_retries=0,
    ))
    coordinator = ExecutionCoordinator(
        task_store=task_store, workflow_store=workflow_store, registry=registry,
        handlers={"demo.transform": lambda payload, deps, ctx: {"result": payload["value"]}},
    )
    outcome = coordinator.run_until_blocked(
        "task_001", principal=principal(), authorization_now_epoch=202,
    )
    current = workflow_store.get_workflow(
        "task_001", principal=principal(), authorization_now_epoch=202,
    )
    assert outcome.status == "running"
    assert "currently holds" in outcome.message
    assert current.steps[0].status == "running"


def test_coordinator_fences_normal_step_execution_and_releases_lease(tmp_path):
    task_store, workflow_store = ready_workflow(tmp_path)
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform demo value",
        description="Coordinator fencing integration test.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=10, max_retries=0,
    ))
    coordinator = ExecutionCoordinator(
        task_store=task_store, workflow_store=workflow_store, registry=registry,
        handlers={"demo.transform": lambda payload, deps, ctx: {"result": payload["value"] + 4}},
    )
    outcome = coordinator.run_until_blocked(
        "task_001", principal=principal(), authorization_now_epoch=202,
    )
    current = workflow_store.get_workflow(
        "task_001", principal=principal(), authorization_now_epoch=202,
    )
    assert outcome.status == "succeeded"
    assert current.steps[0].status == "succeeded"
    assert current.steps[0].output_payload == {"result": 7}
    assert SQLiteWorkerLeaseStore(task_store).get_active(
        "task_001", "step_one", principal=principal(), now_epoch=202,
        authorization_now_epoch=202,
    ) is None
