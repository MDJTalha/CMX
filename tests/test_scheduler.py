import threading
import time

import pytest

from cmx.kernel import CapabilityRegistry, CapabilitySpec, ExecutionPlan, PlanStep
from cmx.orchestration import ExecutionCoordinator
from cmx.persistence import SQLiteTaskStore
from cmx.scheduler import SchedulerConflict, SQLiteWorkflowScheduler
from cmx.security import PrincipalContext
from cmx.workflow_store import SQLiteWorkflowStore


def principal(tenant="tenant_alpha", subject="worker_001"):
    return PrincipalContext(
        subject_id=subject, tenant_id=tenant, issuer="https://identity.cmx.test",
        permissions=frozenset({
            "workflow:submit", "workflow:execute", "workflow:read", "workflow:approve",
            "tasks:create", "tasks:read", "tasks:write", "demo:execute",
        }),
        issued_at=100, expires_at=1000, authentication_verified=True,
        authentication_method="test", credential_id=f"cred_{subject}",
    )


def setup(tmp_path, handler=None):
    tasks = SQLiteTaskStore(tmp_path / "scheduler.sqlite3")
    workflows = SQLiteWorkflowStore(tasks)
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(
        capability_id="demo.transform", name="Transform",
        description="Scheduler test capability.",
        input_schema={"type": "object", "properties": {"value": {"type": "number"}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "number"}},
                       "required": ["result"], "additionalProperties": False},
        required_permissions=["demo:execute"], timeout_seconds=5, max_retries=0,
    ))
    coordinator = ExecutionCoordinator(
        task_store=tasks, workflow_store=workflows, registry=registry,
        handlers={"demo.transform": handler or (lambda payload, deps, ctx: {"result": payload["value"] * 2})},
    )
    plan = ExecutionPlan(
        plan_id="plan_sched_01", objective="Run one scheduled workflow.",
        steps=[PlanStep(step_id="step_one", capability_id="demo.transform",
                        objective="Transform the value.", input_payload={"value": 3})],
    )
    coordinator.submit("task_sched_01", plan, principal=principal(), occurred_at=150,
                       authorization_now_epoch=150)
    return tasks, workflows, coordinator, SQLiteWorkflowScheduler(workflows)


def test_enqueue_is_idempotent_and_run_once_completes_workflow(tmp_path):
    _, _, coordinator, scheduler = setup(tmp_path)
    actor = principal()
    first = scheduler.enqueue("task_sched_01", principal=actor, now_epoch=200)
    second = scheduler.enqueue("task_sched_01", principal=actor, now_epoch=201)
    assert first.job_id == second.job_id
    assert second.status == "queued"

    result = scheduler.run_once(coordinator, worker_id="worker_a", principal=actor,
                                now_epoch=202, lease_seconds=30)
    assert result.status == "succeeded"
    assert result.job.status == "succeeded"
    assert result.outcome.status == "succeeded"
    assert scheduler.get_job("task_sched_01", principal=actor, now_epoch=203).status == "succeeded"


def test_scheduler_jobs_are_tenant_scoped(tmp_path):
    _, _, _, scheduler = setup(tmp_path)
    scheduler.enqueue("task_sched_01", principal=principal(), now_epoch=200)
    other = principal("tenant_other", "other_worker")
    assert scheduler.get_job("task_sched_01", principal=other, now_epoch=200) is None
    with pytest.raises(Exception):
        scheduler.enqueue("task_sched_01", principal=other, now_epoch=200)


def test_expired_job_lease_is_reclaimed_with_new_fencing_token(tmp_path):
    tasks, _, coordinator, scheduler = setup(tmp_path)
    actor = principal()
    job = scheduler.enqueue("task_sched_01", principal=actor, now_epoch=200)
    with tasks._connection() as connection:
        connection.execute(
            """UPDATE cmx_scheduler_jobs SET status='running', attempts=1,
               lease_owner='old_worker', lease_until=205, fencing_token=1
               WHERE job_id=?""",
            (job.job_id,),
        )
    result = scheduler.run_once(coordinator, worker_id="new_worker", principal=actor,
                                now_epoch=205, lease_seconds=30)
    assert result.status == "succeeded"
    assert result.job.fencing_token == 2


def test_concurrent_workers_do_not_claim_the_same_live_job(tmp_path):
    calls = []
    lock = threading.Lock()

    def handler(payload, dependencies, context):
        with lock:
            calls.append(context.step_id)
        time.sleep(0.2)
        return {"result": payload["value"] * 2}

    _, _, coordinator, scheduler = setup(tmp_path, handler)
    actor = principal()
    scheduler.enqueue("task_sched_01", principal=actor, now_epoch=200)
    results = []

    def run(worker):
        results.append(scheduler.run_once(
            coordinator, worker_id=worker, principal=actor, now_epoch=201, lease_seconds=30,
        ))

    first = threading.Thread(target=run, args=("worker_a",))
    second = threading.Thread(target=run, args=("worker_b",))
    first.start()
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert calls == ["step_one"]
    assert sorted(result.status for result in results) == ["idle", "succeeded"]



def test_worker_heartbeat_extends_live_scheduler_lease(tmp_path):
    tasks, _, _, scheduler = setup(tmp_path)
    actor = principal()
    job = scheduler.enqueue("task_sched_01", principal=actor, now_epoch=200)
    now = int(time.time())
    with tasks._connection() as connection:
        connection.execute(
            """UPDATE cmx_scheduler_jobs SET status='running', lease_owner=?,
               lease_until=?, fencing_token=1 WHERE job_id=?""",
            ("heartbeat_worker", now + 4, job.job_id),
        )
        row = connection.execute(
            "SELECT * FROM cmx_scheduler_jobs WHERE job_id=?", (job.job_id,)
        ).fetchone()
    running = scheduler._job(row)
    stop = threading.Event()
    lost = threading.Event()
    heartbeat = threading.Thread(
        target=scheduler._renew_loop,
        args=(running, "heartbeat_worker", 6, stop, lost),
        daemon=True,
    )
    heartbeat.start()
    time.sleep(2.2)
    stop.set()
    heartbeat.join(timeout=2)
    with tasks._connection() as connection:
        renewed = connection.execute(
            "SELECT lease_until FROM cmx_scheduler_jobs WHERE job_id=?", (job.job_id,)
        ).fetchone()["lease_until"]
    assert not heartbeat.is_alive()
    assert not lost.is_set()
    assert renewed > now + 4

def test_failed_or_blocked_job_requires_operator_intervention_before_requeue(tmp_path):
    _, tasks_workflow, coordinator, scheduler = setup(tmp_path)
    actor = principal()
    job = scheduler.enqueue("task_sched_01", principal=actor, now_epoch=200)
    with tasks_workflow.task_store._connection() as connection:
        connection.execute(
            "UPDATE cmx_scheduler_jobs SET status='failed' WHERE job_id=?", (job.job_id,)
        )
    with pytest.raises(SchedulerConflict, match="operator review"):
        scheduler.enqueue("task_sched_01", principal=actor, now_epoch=201)


def test_wall_clock_scheduler_preserves_coordinator_live_clock(tmp_path):
    _, _, coordinator, scheduler = setup(tmp_path)
    now = int(time.time())
    actor = principal().model_copy(update={"issued_at": now - 10, "expires_at": now + 3600})
    scheduler.enqueue("task_sched_01", principal=actor)
    observed = []
    original = coordinator.run_until_blocked

    def capture_clock(task_id, **kwargs):
        observed.append(kwargs.get("authorization_now_epoch"))
        return original(task_id, **kwargs)

    coordinator.run_until_blocked = capture_clock
    result = scheduler.run_once(
        coordinator, worker_id="wall_clock_worker", principal=actor, lease_seconds=30,
    )
    assert result.status == "succeeded"
    # None is intentional: it tells the coordinator to use wall-clock time and
    # run its own worker-lease heartbeat instead of freezing time at claim.
    assert observed == [None]

