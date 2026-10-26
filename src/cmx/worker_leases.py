"""SQLite-backed, tenant-scoped worker reservations with fencing tokens.

Reservations coordinate a single SQLite-backed service. They are not distributed
queue semantics and do not by themselves authorize a handler to complete a workflow
step; coordinator integration must enforce the token at every state transition.
"""
from __future__ import annotations

import re
import time

from pydantic import BaseModel, ConfigDict, Field

from cmx.persistence import SQLiteTaskStore, _validate_identifier
from cmx.security import AuthorizationError, PrincipalContext, authorize_permissions


class WorkerLeaseConflict(RuntimeError):
    """A lease is held by another worker, expired, or fenced by a newer claim."""


class WorkerLeaseNotFound(LookupError):
    """No matching lease exists for the requested tenant-scoped step."""


class WorkerLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    task_id: str
    step_id: str
    worker_id: str
    fencing_token: int = Field(ge=1)
    expires_at: int = Field(ge=0)


_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{1,127}$")


class SQLiteWorkerLeaseStore:
    """Atomic lease reservations for persisted workflow steps on one SQLite node.

    Every claim/reclaim increments a monotonically increasing fencing token. Callers
    must carry that token through downstream operations and reject stale tokens.
    Expired claims may be reassigned, but this class does not replay side effects.
    """

    def __init__(self, task_store: SQLiteTaskStore) -> None:
        self.task_store = task_store
        with self.task_store._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cmx_worker_leases (
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
                    expires_at INTEGER NOT NULL CHECK (expires_at >= 0),
                    updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
                    PRIMARY KEY (tenant_id, task_id, step_id),
                    FOREIGN KEY (tenant_id, task_id, step_id)
                        REFERENCES cmx_workflow_steps (tenant_id, task_id, step_id)
                        ON DELETE RESTRICT
                )
                """
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_cmx_worker_leases_expiry
                   ON cmx_worker_leases (expires_at)"""
            )

    @staticmethod
    def _authorize(
        principal: PrincipalContext | None, now_epoch: int | None
    ) -> PrincipalContext:
        tenant = principal.tenant_id if isinstance(principal, PrincipalContext) else ""
        authorize_permissions(
            principal,
            required_permissions={"workflow:execute", "workflow:read", "tasks:write"},
            resource_tenant_id=tenant,
            now_epoch=now_epoch,
        )
        if not isinstance(principal, PrincipalContext):
            raise AuthorizationError("unauthenticated", "A trusted authenticated identity is required.")
        return principal

    @staticmethod
    def _validate_ids(task_id: str, step_id: str, worker_id: str) -> None:
        _validate_identifier(task_id, "task_id")
        _validate_identifier(step_id, "step_id")
        if not isinstance(worker_id, str) or not _WORKER_ID.fullmatch(worker_id):
            raise ValueError("worker_id has an invalid format.")

    def claim(
        self,
        task_id: str,
        step_id: str,
        *,
        worker_id: str,
        lease_seconds: int,
        principal: PrincipalContext | None,
        now_epoch: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> WorkerLease:
        identity = self._authorize(principal, authorization_now_epoch)
        self._validate_ids(task_id, step_id, worker_id)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be an integer from 1 through 3600.")
        now = int(time.time()) if now_epoch is None else now_epoch
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_epoch must be a non-negative integer.")
        expires = now + lease_seconds
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                step = connection.execute(
                    """SELECT status FROM cmx_workflow_steps
                       WHERE tenant_id=? AND task_id=? AND step_id=?""",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                if step is None:
                    raise WorkerLeaseNotFound("Workflow step was not found in the active tenant.")
                if step["status"] != "pending":
                    raise WorkerLeaseConflict("Only pending workflow steps can be reserved.")
                row = connection.execute(
                    """SELECT worker_id, fencing_token, expires_at FROM cmx_worker_leases
                       WHERE tenant_id=? AND task_id=? AND step_id=?""",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                if row is not None and row["expires_at"] > now:
                    raise WorkerLeaseConflict("The workflow step already has an active worker lease.")
                token = 1 if row is None else int(row["fencing_token"]) + 1
                connection.execute(
                    """INSERT INTO cmx_worker_leases
                       (tenant_id, task_id, step_id, worker_id, fencing_token, expires_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(tenant_id, task_id, step_id) DO UPDATE SET
                         worker_id=excluded.worker_id,
                         fencing_token=excluded.fencing_token,
                         expires_at=excluded.expires_at,
                         updated_at=excluded.updated_at""",
                    (identity.tenant_id, task_id, step_id, worker_id, token, expires, now),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return WorkerLease(
            tenant_id=identity.tenant_id, task_id=task_id, step_id=step_id,
            worker_id=worker_id, fencing_token=token, expires_at=expires,
        )

    def renew(
        self,
        lease: WorkerLease,
        *,
        lease_seconds: int,
        principal: PrincipalContext | None,
        now_epoch: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> WorkerLease:
        identity = self._authorize(principal, authorization_now_epoch)
        if lease.tenant_id != identity.tenant_id:
            raise AuthorizationError("tenant_mismatch", "The lease belongs to another tenant.")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be an integer from 1 through 3600.")
        now = int(time.time()) if now_epoch is None else now_epoch
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_epoch must be a non-negative integer.")
        expires = now + lease_seconds
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE cmx_worker_leases SET expires_at=?, updated_at=?
                       WHERE tenant_id=? AND task_id=? AND step_id=? AND worker_id=?
                         AND fencing_token=? AND expires_at>?""",
                    (expires, now, lease.tenant_id, lease.task_id, lease.step_id,
                     lease.worker_id, lease.fencing_token, now),
                )
                if cursor.rowcount != 1:
                    raise WorkerLeaseConflict("The lease expired or has been superseded.")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return lease.model_copy(update={"expires_at": expires})

    def release(
        self,
        lease: WorkerLease,
        *,
        principal: PrincipalContext | None,
        now_epoch: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> None:
        identity = self._authorize(principal, authorization_now_epoch)
        if lease.tenant_id != identity.tenant_id:
            raise AuthorizationError("tenant_mismatch", "The lease belongs to another tenant.")
        now = int(time.time()) if now_epoch is None else now_epoch
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_epoch must be a non-negative integer.")
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """DELETE FROM cmx_worker_leases
                       WHERE tenant_id=? AND task_id=? AND step_id=? AND worker_id=?
                         AND fencing_token=? AND expires_at>?""",
                    (lease.tenant_id, lease.task_id, lease.step_id,
                     lease.worker_id, lease.fencing_token, now),
                )
                if cursor.rowcount != 1:
                    raise WorkerLeaseConflict("The lease expired or has been superseded.")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
