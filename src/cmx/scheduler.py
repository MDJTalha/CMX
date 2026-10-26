"""Durable, tenant-scoped single-node workflow scheduling.

The scheduler persists enqueue/claim/finish state and uses expiring leases with fencing
tokens. It delegates actual workflow policy and step-level safety to ExecutionCoordinator.
This SQLite queue is not a distributed broker and must not be advertised as multi-node HA.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cmx.orchestration import ExecutionCoordinator, ExecutionOutcome
from cmx.persistence import SQLiteTaskStore, _timestamp, _validate_identifier
from cmx.security import AuthorizationError, PrincipalContext, authorize_permissions
from cmx.workflow_store import SQLiteWorkflowStore, WorkflowNotFoundError


class SchedulerConflict(RuntimeError):
    """A job is leased by another worker or its fencing token is stale."""


class ScheduledJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    tenant_id: str
    task_id: str
    status: Literal["queued", "running", "succeeded", "failed", "blocked"]
    attempts: int = Field(ge=0)
    available_at: int = Field(ge=0)
    lease_owner: str | None = None
    lease_until: int | None = None
    fencing_token: int = Field(ge=0)
    last_error_code: str | None = None


class SchedulerRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["idle", "succeeded", "failed", "blocked", "requeued", "retry_scheduled"]
    job: ScheduledJob | None = None
    outcome: ExecutionOutcome | None = None


class SQLiteWorkflowScheduler:
    """Persistent workflow dispatch queue for one SQLite-backed CMX node."""

    def __init__(self, workflow_store: SQLiteWorkflowStore) -> None:
        self.workflow_store = workflow_store
        self.task_store: SQLiteTaskStore = workflow_store.task_store
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS cmx_scheduler_jobs (
                        job_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN
                            ('queued','running','succeeded','failed','blocked')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                        available_at INTEGER NOT NULL CHECK (available_at >= 0),
                        lease_owner TEXT,
                        lease_until INTEGER,
                        fencing_token INTEGER NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
                        last_error_code TEXT,
                        created_at INTEGER NOT NULL CHECK (created_at >= 0),
                        updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
                        UNIQUE (tenant_id, task_id),
                        CHECK ((status = 'running' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
                            OR (status != 'running' AND lease_owner IS NULL AND lease_until IS NULL))
                    )"""
                )
                connection.execute(
                    """CREATE INDEX IF NOT EXISTS idx_cmx_scheduler_ready
                       ON cmx_scheduler_jobs (tenant_id, status, available_at, created_at)"""
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _authorize(principal: PrincipalContext | None, now: int, *, write: bool) -> PrincipalContext:
        tenant = principal.tenant_id if isinstance(principal, PrincipalContext) else ""
        authorize_permissions(
            principal,
            required_permissions={"workflow:read", "workflow:execute"} if write
            else {"workflow:read"},
            resource_tenant_id=tenant,
            now_epoch=now,
        )
        if not isinstance(principal, PrincipalContext):
            raise AuthorizationError("unauthenticated", "A trusted authenticated identity is required.")
        return principal

    @staticmethod
    def _job(row: sqlite3.Row) -> ScheduledJob:
        return ScheduledJob(
            job_id=row["job_id"], tenant_id=row["tenant_id"], task_id=row["task_id"],
            status=row["status"], attempts=row["attempts"], available_at=row["available_at"],
            lease_owner=row["lease_owner"], lease_until=row["lease_until"],
            fencing_token=row["fencing_token"], last_error_code=row["last_error_code"],
        )

    def enqueue(
        self, task_id: str, *, principal: PrincipalContext,
        now_epoch: int | None = None,
    ) -> ScheduledJob:
        now = _timestamp(now_epoch)
        identity = self._authorize(principal, now, write=True)
        _validate_identifier(task_id, "task_id")
        snapshot = self.workflow_store.get_workflow(
            task_id, principal=identity, authorization_now_epoch=now,
        )
        if snapshot.workflow_state not in {"ready", "running"}:
            raise SchedulerConflict("Only ready or running workflows can be queued.")
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM cmx_scheduler_jobs WHERE tenant_id=? AND task_id=?",
                    (identity.tenant_id, task_id),
                ).fetchone()
                if row is None:
                    job_id = str(uuid.uuid4())
                    connection.execute(
                        """INSERT INTO cmx_scheduler_jobs
                           (job_id, tenant_id, task_id, status, attempts, available_at,
                            fencing_token, created_at, updated_at)
                           VALUES (?, ?, ?, 'queued', 0, ?, 0, ?, ?)""",
                        (job_id, identity.tenant_id, task_id, now, now, now),
                    )
                    row = connection.execute(
                        "SELECT * FROM cmx_scheduler_jobs WHERE job_id=?", (job_id,)
                    ).fetchone()
                elif row["status"] in {"failed", "blocked"}:
                    raise SchedulerConflict("A failed or blocked job requires explicit operator review before requeue.")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._job(row)

    def get_job(
        self, task_id: str, *, principal: PrincipalContext,
        now_epoch: int | None = None,
    ) -> ScheduledJob | None:
        now = _timestamp(now_epoch)
        identity = self._authorize(principal, now, write=False)
        _validate_identifier(task_id, "task_id")
        with self.task_store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM cmx_scheduler_jobs WHERE tenant_id=? AND task_id=?",
                (identity.tenant_id, task_id),
            ).fetchone()
        return None if row is None else self._job(row)

    def run_once(
        self, coordinator: ExecutionCoordinator, *, worker_id: str,
        principal: PrincipalContext, now_epoch: int | None = None,
        lease_seconds: int = 60, max_steps: int = 10, max_attempts: int = 5,
    ) -> SchedulerRunResult:
        now = _timestamp(now_epoch)
        identity = self._authorize(principal, now, write=True)
        _validate_identifier(worker_id, "worker_id")
        for value, label, maximum in (
            (lease_seconds, "lease_seconds", 3600),
            (max_steps, "max_steps", 100),
            (max_attempts, "max_attempts", 20),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{label} must be an integer from 1 through {maximum}.")
        if coordinator.task_store.database_path != self.task_store.database_path:
            raise ValueError("Scheduler and coordinator must use the same SQLite database.")
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """SELECT * FROM cmx_scheduler_jobs
                       WHERE tenant_id=? AND available_at<=?
                         AND (status='queued' OR (status='running' AND lease_until<=?))
                       ORDER BY available_at, created_at LIMIT 1""",
                    (identity.tenant_id, now, now),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return SchedulerRunResult(status="idle")
                if row["attempts"] >= max_attempts:
                    connection.execute(
                        """UPDATE cmx_scheduler_jobs SET status='failed', lease_owner=NULL,
                           lease_until=NULL, last_error_code='attempt_budget_exhausted', updated_at=?
                           WHERE job_id=?""",
                        (now, row["job_id"]),
                    )
                    failed = connection.execute(
                        "SELECT * FROM cmx_scheduler_jobs WHERE job_id=?", (row["job_id"],)
                    ).fetchone()
                    connection.execute("COMMIT")
                    return SchedulerRunResult(status="failed", job=self._job(failed))
                token = int(row["fencing_token"]) + 1
                connection.execute(
                    """UPDATE cmx_scheduler_jobs SET status='running', attempts=attempts+1,
                       lease_owner=?, lease_until=?, fencing_token=?, updated_at=?
                       WHERE job_id=?""",
                    (worker_id, now + lease_seconds, token, now, row["job_id"]),
                )
                claimed = connection.execute(
                    "SELECT * FROM cmx_scheduler_jobs WHERE job_id=?", (row["job_id"],)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        job = self._job(claimed)
        try:
            outcome = coordinator.run_until_blocked(
                job.task_id, principal=identity, authorization_now_epoch=now, max_steps=max_steps,
            )
            if outcome.status == "succeeded":
                return self._finish(job, identity, worker_id, "succeeded", None, now, outcome)
            if outcome.status in {"failed", "cancelled"}:
                return self._finish(job, identity, worker_id, "failed", "workflow_terminal_failure", now, outcome)
            if outcome.status == "awaiting_approval":
                return self._finish(job, identity, worker_id, "blocked", "approval_required", now, outcome)
            return self._requeue(job, identity, worker_id, now, outcome)
        except Exception:
            return self._retry_or_fail(job, identity, worker_id, now)

    def _finish(
        self, job: ScheduledJob, principal: PrincipalContext, worker_id: str,
        status: Literal["succeeded", "failed", "blocked"], error_code: str | None,
        now: int, outcome: ExecutionOutcome,
    ) -> SchedulerRunResult:
        updated = self._update_owned(job, principal, worker_id, status, error_code, now, now)
        return SchedulerRunResult(status=status, job=updated, outcome=outcome)

    def _requeue(
        self, job: ScheduledJob, principal: PrincipalContext, worker_id: str,
        now: int, outcome: ExecutionOutcome,
    ) -> SchedulerRunResult:
        updated = self._update_owned(job, principal, worker_id, "queued", None, now + 1, now)
        return SchedulerRunResult(status="requeued", job=updated, outcome=outcome)

    def _retry_or_fail(
        self, job: ScheduledJob, principal: PrincipalContext, worker_id: str, now: int,
    ) -> SchedulerRunResult:
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """SELECT * FROM cmx_scheduler_jobs
                       WHERE tenant_id=? AND job_id=?""",
                    (principal.tenant_id, job.job_id),
                ).fetchone()
                if row is None or row["status"] != "running" or row["lease_owner"] != worker_id or row["fencing_token"] != job.fencing_token:
                    raise SchedulerConflict("Scheduler lease was lost; stale worker cannot update the job.")
                terminal = row["attempts"] >= 5
                status = "failed" if terminal else "queued"
                delay = min(60, 2 ** min(row["attempts"], 6))
                connection.execute(
                    """UPDATE cmx_scheduler_jobs SET status=?, available_at=?, lease_owner=NULL,
                       lease_until=NULL, last_error_code='worker_error', updated_at=?
                       WHERE job_id=? AND fencing_token=?""",
                    (status, now if terminal else now + delay, now, job.job_id, job.fencing_token),
                )
                result = connection.execute(
                    "SELECT * FROM cmx_scheduler_jobs WHERE job_id=?", (job.job_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return SchedulerRunResult(status="failed" if result["status"] == "failed" else "retry_scheduled",
                                  job=self._job(result))

    def _update_owned(
        self, job: ScheduledJob, principal: PrincipalContext, worker_id: str,
        status: Literal["queued", "succeeded", "failed", "blocked"],
        error_code: str | None, available_at: int, now: int,
    ) -> ScheduledJob:
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                updated = connection.execute(
                    """UPDATE cmx_scheduler_jobs SET status=?, available_at=?, lease_owner=NULL,
                       lease_until=NULL, last_error_code=?, updated_at=?
                       WHERE tenant_id=? AND job_id=? AND status='running'
                         AND lease_owner=? AND fencing_token=? AND lease_until>?""",
                    (status, available_at, error_code, now, principal.tenant_id,
                     job.job_id, worker_id, job.fencing_token, now),
                )
                if updated.rowcount != 1:
                    raise SchedulerConflict("Scheduler lease expired or was fenced; stale worker cannot commit.")
                row = connection.execute(
                    "SELECT * FROM cmx_scheduler_jobs WHERE job_id=?", (job.job_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._job(row)
