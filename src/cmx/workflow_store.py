"""Durable workflow-plan, step-result, approval and workflow-audit storage.

This store shares the SQLite database used by SQLiteTaskStore. Workflow records and
step transitions are tenant-scoped; hashes make accidental or partial audit tampering
detectable. It remains a single-node reference backend, not a distributed executor.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cmx.kernel import ExecutionPlan, PlanStep, TaskRecord, TaskState
from cmx.persistence import (
    AuditEventRecord,
    AuditIntegrityError,
    SQLiteTaskStore,
    TaskAlreadyExistsError,
    _canonical_json,
    _event_hash,
    _timestamp,
    _validate_identifier,
)
from cmx.security import AuthorizationError, PrincipalContext, authorize_permissions

WorkflowState = Literal["ready", "running", "awaiting_approval", "succeeded", "failed", "cancelled"]
StepStatus = Literal[
    "pending", "awaiting_approval", "running", "succeeded", "failed", "blocked", "interrupted"
]
ZERO_HASH = "0" * 64
_SCHEMA_VERSION = 1
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")


class WorkflowNotFoundError(LookupError):
    """The requested workflow is not visible in the principal's tenant."""


class WorkflowAlreadyExistsError(ValueError):
    """A workflow with this plan identifier already exists in the active tenant."""


class WorkflowIntegrityError(RuntimeError):
    """Persisted workflow data or its audit stream fails integrity checks."""


class WorkflowConflictError(ValueError):
    """The requested workflow or step transition is invalid in the current state."""


class StepApprovalRequired(PermissionError):
    """A step with an approval requirement has no valid approval bound to its inputs."""


class WorkflowStepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    ordinal: int = Field(ge=0)
    capability_id: str
    capability_version: str
    capability_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: StepStatus
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=1, le=5)
    requires_approval: bool
    input_payload: dict[str, Any]
    output_payload: Any = None
    error_code: str | None = None
    approval_requested_by: str | None = None
    approved_by: str | None = None
    approved_at: int | None = None
    approval_fingerprint: str | None = None
    updated_at: int = Field(ge=0)


class WorkflowSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: TaskRecord
    plan: ExecutionPlan
    workflow_state: WorkflowState
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    steps: tuple[WorkflowStepRecord, ...]


def _approval_fingerprint(
    *,
    tenant_id: str,
    task_id: str,
    plan_hash: str,
    step: PlanStep,
    capability_version: str,
    capability_fingerprint: str,
) -> str:
    body = {
        "tenant_id": tenant_id,
        "task_id": task_id,
        "plan_hash": plan_hash,
        "step_id": step.step_id,
        "capability_id": step.capability_id,
        "capability_version": capability_version,
        "capability_fingerprint": capability_fingerprint,
        "requires_approval": step.requires_approval,
        "max_attempts": step.max_attempts,
        "input_payload": step.input_payload,
    }
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _payload_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class SQLiteWorkflowStore:
    """Store immutable plan definitions and mutable step state beside durable task data."""

    def __init__(self, task_store: SQLiteTaskStore) -> None:
        self.task_store = task_store
        self.database_path = task_store.database_path
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cmx_workflow_schema_meta (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        version INTEGER NOT NULL CHECK (version >= 1)
                    )
                    """
                )
                connection.execute(
                    "INSERT OR IGNORE INTO cmx_workflow_schema_meta (singleton, version) VALUES (1, 1)"
                )
                version = int(
                    connection.execute(
                        "SELECT version FROM cmx_workflow_schema_meta WHERE singleton = 1"
                    ).fetchone()[0]
                )
                if version > _SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Workflow schema version {version} is newer than supported version {_SCHEMA_VERSION}."
                    )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cmx_workflow_plans (
                        tenant_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        plan_id TEXT NOT NULL,
                        objective TEXT NOT NULL,
                        plan_json TEXT NOT NULL,
                        plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64),
                        state TEXT NOT NULL CHECK (
                            state IN ('ready','running','awaiting_approval','succeeded','failed','cancelled')
                        ),
                        created_at INTEGER NOT NULL CHECK (created_at >= 0),
                        updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
                        PRIMARY KEY (tenant_id, task_id),
                        UNIQUE (tenant_id, plan_id),
                        FOREIGN KEY (tenant_id, task_id)
                            REFERENCES cmx_tasks (tenant_id, task_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cmx_workflow_steps (
                        tenant_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        step_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                        capability_id TEXT NOT NULL,
                        capability_version TEXT NOT NULL,
                        capability_fingerprint TEXT NOT NULL CHECK (length(capability_fingerprint) = 64),
                        status TEXT NOT NULL CHECK (
                            status IN ('pending','awaiting_approval','running','succeeded','failed','blocked','interrupted')
                        ),
                        attempts INTEGER NOT NULL CHECK (attempts >= 0),
                        max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 5),
                        requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0,1)),
                        input_json TEXT NOT NULL,
                        output_json TEXT,
                        error_code TEXT,
                        approval_requested_by TEXT,
                        approved_by TEXT,
                        approved_at INTEGER,
                        approval_fingerprint TEXT,
                        updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
                        PRIMARY KEY (tenant_id, task_id, step_id),
                        UNIQUE (tenant_id, task_id, ordinal),
                        FOREIGN KEY (tenant_id, task_id)
                            REFERENCES cmx_workflow_plans (tenant_id, task_id)
                            ON DELETE RESTRICT,
                        CHECK ((approved_by IS NULL AND approved_at IS NULL AND approval_fingerprint IS NULL)
                            OR (approved_by IS NOT NULL AND approved_at IS NOT NULL
                                AND length(approval_fingerprint) = 64))
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_cmx_workflow_steps_status
                    ON cmx_workflow_steps (tenant_id, task_id, status, ordinal)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cmx_workflow_events (
                        event_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL CHECK (sequence >= 0),
                        event_type TEXT NOT NULL,
                        occurred_at INTEGER NOT NULL CHECK (occurred_at >= 0),
                        actor_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        previous_hash TEXT NOT NULL CHECK (length(previous_hash) = 64),
                        event_hash TEXT NOT NULL CHECK (length(event_hash) = 64),
                        UNIQUE (tenant_id, task_id, sequence),
                        FOREIGN KEY (tenant_id, task_id)
                            REFERENCES cmx_workflow_plans (tenant_id, task_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS cmx_workflow_events_no_update
                    BEFORE UPDATE ON cmx_workflow_events
                    BEGIN SELECT RAISE(ABORT, 'workflow audit events are immutable'); END
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS cmx_workflow_events_no_delete
                    BEFORE DELETE ON cmx_workflow_events
                    BEGIN SELECT RAISE(ABORT, 'workflow audit events are immutable'); END
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS cmx_workflow_plan_definition_immutable
                    BEFORE UPDATE ON cmx_workflow_plans
                    WHEN NEW.tenant_id IS NOT OLD.tenant_id
                      OR NEW.task_id IS NOT OLD.task_id
                      OR NEW.plan_id IS NOT OLD.plan_id
                      OR NEW.objective IS NOT OLD.objective
                      OR NEW.plan_json IS NOT OLD.plan_json
                      OR NEW.plan_hash IS NOT OLD.plan_hash
                      OR NEW.created_at IS NOT OLD.created_at
                    BEGIN SELECT RAISE(ABORT, 'workflow plan definition is immutable'); END
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS cmx_workflow_step_definition_immutable
                    BEFORE UPDATE ON cmx_workflow_steps
                    WHEN NEW.tenant_id IS NOT OLD.tenant_id
                      OR NEW.task_id IS NOT OLD.task_id
                      OR NEW.step_id IS NOT OLD.step_id
                      OR NEW.ordinal IS NOT OLD.ordinal
                      OR NEW.capability_id IS NOT OLD.capability_id
                      OR NEW.capability_version IS NOT OLD.capability_version
                      OR NEW.max_attempts IS NOT OLD.max_attempts
                      OR NEW.requires_approval IS NOT OLD.requires_approval
                      OR NEW.input_json IS NOT OLD.input_json
                    BEGIN SELECT RAISE(ABORT, 'workflow step definition is immutable'); END
                    """
                )
                connection.execute(
                    "UPDATE cmx_workflow_schema_meta SET version = ? WHERE singleton = 1",
                    (_SCHEMA_VERSION,),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _authorize(
        principal: PrincipalContext | None,
        permissions: set[str] | frozenset[str],
        now_epoch: int | None,
    ) -> PrincipalContext:
        tenant = principal.tenant_id if isinstance(principal, PrincipalContext) else ""
        authorize_permissions(
            principal,
            required_permissions=permissions,
            resource_tenant_id=tenant,
            now_epoch=now_epoch,
        )
        if not isinstance(principal, PrincipalContext):
            raise AuthorizationError("unauthenticated", "A trusted authenticated identity is required.")
        return principal

    @staticmethod
    def _event_rows(
        connection: sqlite3.Connection,
        tenant_id: str,
        task_id: str,
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT event_id, tenant_id, task_id, sequence, event_type, occurred_at,
                       actor_id, payload_json, previous_hash, event_hash
                FROM cmx_workflow_events
                WHERE tenant_id = ? AND task_id = ?
                ORDER BY sequence ASC
                """,
                (tenant_id, task_id),
            ).fetchall()
        )

    @staticmethod
    def _decode_event_payload(row: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise WorkflowIntegrityError("Workflow audit stream contains invalid JSON.") from exc
        if not isinstance(value, dict):
            raise WorkflowIntegrityError("Workflow audit event payload must be an object.")
        return value

    @classmethod
    def _verify_event_rows(cls, rows: Sequence[sqlite3.Row]) -> bool:
        previous_hash = ZERO_HASH
        previous_time = -1
        for sequence, row in enumerate(rows):
            if row["sequence"] != sequence or row["previous_hash"] != previous_hash:
                return False
            if row["occurred_at"] < previous_time:
                return False
            try:
                payload = cls._decode_event_payload(row)
                calculated = _event_hash(
                    {
                        "event_id": row["event_id"],
                        "tenant_id": row["tenant_id"],
                        "task_id": row["task_id"],
                        "sequence": row["sequence"],
                        "event_type": row["event_type"],
                        "occurred_at": row["occurred_at"],
                        "actor_id": row["actor_id"],
                        "payload": payload,
                        "previous_hash": row["previous_hash"],
                    }
                )
            except (ValueError, WorkflowIntegrityError):
                return False
            if not hmac.compare_digest(calculated, row["event_hash"]):
                return False
            previous_hash = row["event_hash"]
            previous_time = row["occurred_at"]
        return bool(rows) and rows[0]["event_type"] == "workflow.created"

    @classmethod
    def _append_event(
        cls,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        task_id: str,
        event_type: str,
        occurred_at: int,
        actor_id: str,
        payload: dict[str, Any],
    ) -> AuditEventRecord:
        _validate_identifier(actor_id, "actor_id")
        if not _EVENT_TYPE_RE.fullmatch(event_type):
            raise ValueError("Workflow event type has an invalid format.")
        existing = cls._event_rows(connection, tenant_id, task_id)
        if existing and not cls._verify_event_rows(existing):
            raise WorkflowIntegrityError("Existing workflow audit stream failed its integrity check.")
        if existing and occurred_at < existing[-1]["occurred_at"]:
            raise ValueError("Workflow audit event timestamps must not move backwards.")
        sequence = len(existing)
        event_id = str(uuid.uuid4())
        previous_hash = existing[-1]["event_hash"] if existing else ZERO_HASH
        payload_json = _canonical_json(payload)
        fields = {
            "event_id": event_id,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "actor_id": actor_id,
            "payload": payload,
            "previous_hash": previous_hash,
        }
        digest = _event_hash(fields)
        connection.execute(
            """
            INSERT INTO cmx_workflow_events (
                event_id, tenant_id, task_id, sequence, event_type, occurred_at,
                actor_id, payload_json, previous_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, tenant_id, task_id, sequence, event_type, occurred_at,
                actor_id, payload_json, previous_hash, digest,
            ),
        )
        return AuditEventRecord(**fields, event_hash=digest)

    @staticmethod
    def _step_projection(row: sqlite3.Row) -> dict[str, Any]:
        output_hash: str | None = None
        if row["output_json"] is not None:
            try:
                output = json.loads(row["output_json"])
                output_hash = _payload_hash(output)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise WorkflowIntegrityError("Workflow step output is not valid canonical JSON.") from exc
        return {
            "status": row["status"],
            "capability_fingerprint": row["capability_fingerprint"],
            "attempts": row["attempts"],
            "error_code": row["error_code"],
            "approval_requested_by": row["approval_requested_by"],
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
            "approval_fingerprint": row["approval_fingerprint"],
            "output_hash": output_hash,
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _step_rows(
        connection: sqlite3.Connection,
        tenant_id: str,
        task_id: str,
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT tenant_id, task_id, step_id, ordinal, capability_id, capability_version,
                       capability_fingerprint, status, attempts, max_attempts, requires_approval, input_json, output_json,
                       error_code, approval_requested_by, approved_by, approved_at,
                       approval_fingerprint, updated_at
                FROM cmx_workflow_steps
                WHERE tenant_id = ? AND task_id = ?
                ORDER BY ordinal ASC
                """,
                (tenant_id, task_id),
            ).fetchall()
        )

    @classmethod
    def _verify_workflow_rows(
        cls,
        plan_row: sqlite3.Row,
        step_rows: Sequence[sqlite3.Row],
        event_rows: Sequence[sqlite3.Row],
    ) -> tuple[ExecutionPlan, dict[str, Any]] | None:
        try:
            decoded = json.loads(plan_row["plan_json"])
            canonical = _canonical_json(decoded)
            if canonical != plan_row["plan_json"]:
                return None
            plan_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(plan_hash, plan_row["plan_hash"]):
                return None
            plan = ExecutionPlan.model_validate(decoded)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

        if not cls._verify_event_rows(event_rows) or len(step_rows) != len(plan.steps):
            return None

        projected_steps: dict[str, dict[str, Any]] = {}
        workflow_state: str | None = None
        for index, row in enumerate(event_rows):
            payload = cls._decode_event_payload(row)
            if index == 0:
                if (
                    payload.get("workflow_state") != "ready"
                    or payload.get("plan_hash") != plan_hash
                    or payload.get("step_count") != len(plan.steps)
                    or not isinstance(payload.get("steps"), dict)
                ):
                    return None
                projected_steps = payload["steps"]
            else:
                workflow_state = payload.get("workflow_state", workflow_state)
                step_id = payload.get("step_id")
                step_projection = payload.get("step")
                if step_id is not None:
                    if not isinstance(step_projection, dict):
                        return None
                    projected_steps[step_id] = step_projection
            if payload.get("workflow_state") is not None:
                workflow_state = payload["workflow_state"]

        if workflow_state != plan_row["state"]:
            return None

        expected_plan_steps = list(plan.steps)
        for ordinal, (spec, row) in enumerate(zip(expected_plan_steps, step_rows)):
            if (
                row["ordinal"] != ordinal
                or row["step_id"] != spec.step_id
                or row["capability_id"] != spec.capability_id
                or row["max_attempts"] != spec.max_attempts
                or bool(row["requires_approval"]) != spec.requires_approval
                or row["input_json"] != _canonical_json(spec.input_payload)
            ):
                return None
            projection = projected_steps.get(spec.step_id)
            if not isinstance(projection, dict) or projection != cls._step_projection(row):
                return None
        if set(projected_steps) != {step.step_id for step in plan.steps}:
            return None
        return plan, {"plan_hash": plan_hash, "workflow_state": workflow_state}

    @staticmethod
    def _plan_row(
        connection: sqlite3.Connection, tenant_id: str, task_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT tenant_id, task_id, plan_id, objective, plan_json, plan_hash, state, created_at, updated_at
            FROM cmx_workflow_plans WHERE tenant_id = ? AND task_id = ?
            """,
            (tenant_id, task_id),
        ).fetchone()

    @classmethod
    def _assert_workflow_integrity(
        cls,
        connection: sqlite3.Connection,
        tenant_id: str,
        task_id: str,
    ) -> tuple[sqlite3.Row, list[sqlite3.Row], list[sqlite3.Row], ExecutionPlan]:
        plan_row = cls._plan_row(connection, tenant_id, task_id)
        if plan_row is None:
            raise WorkflowNotFoundError("Workflow was not found in the active tenant.")
        step_rows = cls._step_rows(connection, tenant_id, task_id)
        event_rows = cls._event_rows(connection, tenant_id, task_id)
        checked = cls._verify_workflow_rows(plan_row, step_rows, event_rows)
        if checked is None:
            raise WorkflowIntegrityError("Workflow definition, step state or audit history failed integrity checks.")
        return plan_row, step_rows, event_rows, checked[0]

    @staticmethod
    def _row_to_step(row: sqlite3.Row) -> WorkflowStepRecord:
        try:
            input_payload = json.loads(row["input_json"])
            output_payload = json.loads(row["output_json"]) if row["output_json"] is not None else None
        except (json.JSONDecodeError, TypeError) as exc:
            raise WorkflowIntegrityError("Workflow step contains invalid JSON.") from exc
        return WorkflowStepRecord(
            step_id=row["step_id"],
            ordinal=row["ordinal"],
            capability_id=row["capability_id"],
            capability_version=row["capability_version"],
            capability_fingerprint=row["capability_fingerprint"],
            status=row["status"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            requires_approval=bool(row["requires_approval"]),
            input_payload=input_payload,
            output_payload=output_payload,
            error_code=row["error_code"],
            approval_requested_by=row["approval_requested_by"],
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            approval_fingerprint=row["approval_fingerprint"],
            updated_at=row["updated_at"],
        )

    def create_workflow(
        self,
        task: TaskRecord,
        plan: ExecutionPlan,
        *,
        principal: PrincipalContext | None,
        capability_versions: Mapping[str, str],
        capability_fingerprints: Mapping[str, str],
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> WorkflowSnapshot:
        """Create task, immutable plan, initial steps and both audit records atomically."""
        identity = self._authorize(
            principal,
            {"workflow:submit", "workflow:read", "tasks:create", "tasks:read"},
            authorization_now_epoch,
        )
        timestamp = _timestamp(occurred_at)
        if task.state != TaskState.CREATED or task.version != 0 or task.attempts != 1 or task.last_event is not None:
            raise ValueError("New workflow tasks must start in created state at version 0 and attempt 1.")
        required_caps = {step.capability_id for step in plan.steps}
        if set(capability_versions) != required_caps or set(capability_fingerprints) != required_caps:
            raise ValueError("Capability versions and fingerprints must cover exactly the plan capabilities.")
        for version in capability_versions.values():
            if not re.fullmatch(r"^\d+\.\d+\.\d+$", version):
                raise ValueError("Capability versions must use semantic version format.")
        for fingerprint in capability_fingerprints.values():
            if not re.fullmatch(r"^[0-9a-f]{64}$", fingerprint):
                raise ValueError("Capability fingerprints must be lowercase SHA-256 hashes.")

        plan_data = plan.model_dump(mode="json")
        plan_json = _canonical_json(plan_data)
        plan_hash = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        try:
            with self.task_store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        """
                        INSERT INTO cmx_tasks (
                            tenant_id, task_id, state, version, attempts,
                            max_attempts, last_event, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            identity.tenant_id, task.task_id, task.state.value, task.version,
                            task.attempts, task.max_attempts, None, timestamp, timestamp,
                        ),
                    )
                    SQLiteTaskStore._append_audit_event(
                        connection,
                        tenant_id=identity.tenant_id,
                        task_id=task.task_id,
                        sequence=0,
                        event_type="task.created",
                        occurred_at=timestamp,
                        actor_id=identity.subject_id,
                        payload={
                            "state": task.state.value,
                            "version": task.version,
                            "attempts": task.attempts,
                            "max_attempts": task.max_attempts,
                            "last_event": None,
                        },
                    )
                    connection.execute(
                        """
                        INSERT INTO cmx_workflow_plans (
                            tenant_id, task_id, plan_id, objective, plan_json,
                            plan_hash, state, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?)
                        """,
                        (
                            identity.tenant_id, task.task_id, plan.plan_id, plan.objective,
                            plan_json, plan_hash, timestamp, timestamp,
                        ),
                    )
                    for ordinal, step in enumerate(plan.steps):
                        connection.execute(
                            """
                            INSERT INTO cmx_workflow_steps (
                                tenant_id, task_id, step_id, ordinal, capability_id,
                                capability_version, capability_fingerprint, status, attempts, max_attempts,
                                requires_approval, input_json, output_json, error_code,
                                approval_requested_by, approved_by, approved_at,
                                approval_fingerprint, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?)
                            """,
                            (
                                identity.tenant_id, task.task_id, step.step_id, ordinal,
                                step.capability_id, capability_versions[step.capability_id],
                                capability_fingerprints[step.capability_id],
                                step.max_attempts, int(step.requires_approval),
                                _canonical_json(step.input_payload), timestamp,
                            ),
                        )
                    rows = self._step_rows(connection, identity.tenant_id, task.task_id)
                    initial_steps = {
                        row["step_id"]: self._step_projection(row)
                        for row in rows
                    }
                    self._append_event(
                        connection,
                        tenant_id=identity.tenant_id,
                        task_id=task.task_id,
                        event_type="workflow.created",
                        occurred_at=timestamp,
                        actor_id=identity.subject_id,
                        payload={
                            "workflow_state": "ready",
                            "plan_hash": plan_hash,
                            "step_count": len(plan.steps),
                            "steps": initial_steps,
                        },
                    )
                    connection.execute("COMMIT")
                except sqlite3.IntegrityError as exc:
                    connection.execute("ROLLBACK")
                    message = str(exc)
                    if "cmx_tasks.tenant_id, cmx_tasks.task_id" in message:
                        raise TaskAlreadyExistsError(
                            "A task with this ID already exists in the active tenant."
                        ) from exc
                    if "cmx_workflow_plans.tenant_id, cmx_workflow_plans.plan_id" in message:
                        raise WorkflowAlreadyExistsError(
                            "A workflow with this plan ID already exists in the active tenant."
                        ) from exc
                    raise
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.IntegrityError:
            raise
        return self.get_workflow(
            task.task_id,
            principal=identity,
            authorization_now_epoch=authorization_now_epoch,
        )

    def get_workflow(
        self,
        task_id: str,
        *,
        principal: PrincipalContext | None,
        authorization_now_epoch: int | None = None,
    ) -> WorkflowSnapshot:
        identity = self._authorize(
            principal, {"workflow:read", "tasks:read"}, authorization_now_epoch
        )
        _validate_identifier(task_id, "task_id")
        with self.task_store._connection() as connection:
            plan_row = self._plan_row(connection, identity.tenant_id, task_id)
            if plan_row is None:
                raise WorkflowNotFoundError("Workflow was not found in the active tenant.")
            steps = self._step_rows(connection, identity.tenant_id, task_id)
            events = self._event_rows(connection, identity.tenant_id, task_id)
            checked = self._verify_workflow_rows(plan_row, steps, events)
        if checked is None:
            raise WorkflowIntegrityError("Workflow definition, step state or audit history failed integrity checks.")

        task = self.task_store.get_task(
            task_id, principal=identity, authorization_now_epoch=authorization_now_epoch
        )
        if task is None:
            raise WorkflowIntegrityError("Workflow exists without its corresponding task record.")
        plan, info = checked
        return WorkflowSnapshot(
            task=task,
            plan=plan,
            workflow_state=info["workflow_state"],
            plan_hash=info["plan_hash"],
            steps=tuple(self._row_to_step(row) for row in steps),
        )

    def list_workflow_events(
        self,
        task_id: str,
        *,
        principal: PrincipalContext | None,
        limit: int = 500,
        authorization_now_epoch: int | None = None,
    ) -> list[AuditEventRecord]:
        identity = self._authorize(
            principal, {"workflow:read", "audit:read"}, authorization_now_epoch
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000.")
        _validate_identifier(task_id, "task_id")
        with self.task_store._connection() as connection:
            rows = self._event_rows(connection, identity.tenant_id, task_id)
        if not self._verify_event_rows(rows):
            raise WorkflowIntegrityError("Workflow audit history failed integrity checks.")
        return [
            AuditEventRecord(
                event_id=row["event_id"],
                tenant_id=row["tenant_id"],
                task_id=row["task_id"],
                sequence=row["sequence"],
                event_type=row["event_type"],
                occurred_at=row["occurred_at"],
                actor_id=row["actor_id"],
                payload=self._decode_event_payload(row),
                previous_hash=row["previous_hash"],
                event_hash=row["event_hash"],
            )
            for row in rows[:limit]
        ]

    @classmethod
    def _record_step_event(
        cls,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        task_id: str,
        actor_id: str,
        occurred_at: int,
        event_type: str,
        workflow_state: str,
        step_id: str | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        plan_row = cls._plan_row(connection, tenant_id, task_id)
        if plan_row is None:
            raise WorkflowNotFoundError("Workflow was not found in the active tenant.")
        if step_id is None:
            payload = {"workflow_state": workflow_state}
        else:
            row = connection.execute(
                """
                SELECT step_id, status, attempts, output_json, error_code,
                       approval_requested_by, approved_by, approved_at,
                       approval_fingerprint, updated_at
                FROM cmx_workflow_steps
                WHERE tenant_id = ? AND task_id = ? AND step_id = ?
                """,
                (tenant_id, task_id, step_id),
            ).fetchone()
            if row is None:
                raise WorkflowConflictError("Workflow step was not found.")
            payload = {
                "workflow_state": workflow_state,
                "step_id": step_id,
                "step": cls._step_projection(row),
            }
        if extra_payload:
            payload["details"] = extra_payload
        cls._append_event(
            connection,
            tenant_id=tenant_id,
            task_id=task_id,
            event_type=event_type,
            occurred_at=occurred_at,
            actor_id=actor_id,
            payload=payload,
        )

    @staticmethod
    def _transition_workflow_state(current: str, target: str) -> None:
        allowed = {
            "ready": {"running", "failed", "cancelled"},
            "running": {"awaiting_approval", "succeeded", "failed", "cancelled"},
            "awaiting_approval": {"running", "failed", "cancelled"},
            "succeeded": set(),
            "failed": set(),
            "cancelled": set(),
        }
        if target == current:
            return
        if target not in allowed.get(current, set()):
            raise WorkflowConflictError(
                f"Workflow cannot transition from '{current}' to '{target}'."
            )

    @classmethod
    def _write_workflow_state(
        cls,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        task_id: str,
        target: str,
        actor_id: str,
        occurred_at: int,
    ) -> str:
        row = cls._plan_row(connection, tenant_id, task_id)
        if row is None:
            raise WorkflowNotFoundError("Workflow was not found in the active tenant.")
        current = row["state"]
        cls._transition_workflow_state(current, target)
        if current != target:
            connection.execute(
                """
                UPDATE cmx_workflow_plans SET state = ?, updated_at = ?
                WHERE tenant_id = ? AND task_id = ?
                """,
                (target, occurred_at, tenant_id, task_id),
            )
        return target

    def set_workflow_state(
        self,
        task_id: str,
        target: WorkflowState,
        *,
        principal: PrincipalContext | None,
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> None:
        identity = self._authorize(
            principal, {"workflow:execute", "workflow:read"}, authorization_now_epoch
        )
        timestamp = _timestamp(occurred_at)
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_workflow_integrity(connection, identity.tenant_id, task_id)
                row = self._plan_row(connection, identity.tenant_id, task_id)
                if row is None:
                    raise WorkflowNotFoundError("Workflow was not found in the active tenant.")
                if row["state"] != target:
                    self._write_workflow_state(
                        connection, tenant_id=identity.tenant_id, task_id=task_id,
                        target=target, actor_id=identity.subject_id, occurred_at=timestamp,
                    )
                    self._record_step_event(
                        connection, tenant_id=identity.tenant_id, task_id=task_id,
                        actor_id=identity.subject_id, occurred_at=timestamp,
                        event_type="workflow.state", workflow_state=target,
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def request_step_approval(
        self,
        task_id: str,
        step_id: str,
        *,
        principal: PrincipalContext | None,
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> WorkflowStepRecord:
        identity = self._authorize(
            principal, {"workflow:execute", "tasks:write"}, authorization_now_epoch
        )
        timestamp = _timestamp(occurred_at)
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_workflow_integrity(connection, identity.tenant_id, task_id)
                plan_row = self._plan_row(connection, identity.tenant_id, task_id)
                row = connection.execute(
                    """
                    SELECT * FROM cmx_workflow_steps
                    WHERE tenant_id = ? AND task_id = ? AND step_id = ?
                    """,
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                if plan_row is None or row is None:
                    raise WorkflowNotFoundError("Workflow step was not found in the active tenant.")
                if not bool(row["requires_approval"]):
                    raise WorkflowConflictError("This step does not require a separate approval.")
                if row["status"] == "awaiting_approval":
                    result = self._row_to_step(row)
                    connection.execute("COMMIT")
                    return result
                if row["status"] != "pending":
                    raise WorkflowConflictError("Only a pending step can request approval.")
                state = self._write_workflow_state(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    target="awaiting_approval", actor_id=identity.subject_id, occurred_at=timestamp,
                )
                connection.execute(
                    """
                    UPDATE cmx_workflow_steps
                    SET status = 'awaiting_approval', approval_requested_by = ?, updated_at = ?
                    WHERE tenant_id = ? AND task_id = ? AND step_id = ? AND status = 'pending'
                    """,
                    (identity.subject_id, timestamp, identity.tenant_id, task_id, step_id),
                )
                self._record_step_event(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    actor_id=identity.subject_id, occurred_at=timestamp,
                    event_type="step.approval_requested", workflow_state=state, step_id=step_id,
                )
                final = connection.execute(
                    "SELECT * FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._row_to_step(final)

    def approve_step(
        self,
        task_id: str,
        step_id: str,
        *,
        principal: PrincipalContext | None,
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> WorkflowStepRecord:
        identity = self._authorize(
            principal, {"workflow:approve", "workflow:read"}, authorization_now_epoch
        )
        timestamp = _timestamp(occurred_at)
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                plan_row = self._plan_row(connection, identity.tenant_id, task_id)
                row = connection.execute(
                    "SELECT * FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                if plan_row is None or row is None:
                    raise WorkflowNotFoundError("Workflow step was not found in the active tenant.")
                if row["status"] != "awaiting_approval" or not bool(row["requires_approval"]):
                    raise WorkflowConflictError("Step is not awaiting approval.")
                plan = ExecutionPlan.model_validate_json(plan_row["plan_json"])
                step_spec = next((item for item in plan.steps if item.step_id == step_id), None)
                if step_spec is None:
                    raise WorkflowIntegrityError("Approval references a step absent from the immutable plan.")
                fingerprint = _approval_fingerprint(
                    tenant_id=identity.tenant_id,
                    task_id=task_id,
                    plan_hash=plan_row["plan_hash"],
                    step=step_spec,
                    capability_version=row["capability_version"],
                    capability_fingerprint=row["capability_fingerprint"],
                )
                connection.execute(
                    """
                    UPDATE cmx_workflow_steps
                    SET status = 'pending', approved_by = ?, approved_at = ?,
                        approval_fingerprint = ?, updated_at = ?
                    WHERE tenant_id = ? AND task_id = ? AND step_id = ?
                    """,
                    (
                        identity.subject_id, timestamp, fingerprint, timestamp,
                        identity.tenant_id, task_id, step_id,
                    ),
                )
                state = self._write_workflow_state(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    target="running", actor_id=identity.subject_id, occurred_at=timestamp,
                )
                self._record_step_event(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    actor_id=identity.subject_id, occurred_at=timestamp,
                    event_type="step.approved", workflow_state=state, step_id=step_id,
                )
                final = connection.execute(
                    "SELECT * FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._row_to_step(final)

    def start_step(
        self,
        task_id: str,
        step_id: str,
        *,
        principal: PrincipalContext | None,
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> WorkflowStepRecord:
        identity = self._authorize(
            principal, {"workflow:execute", "tasks:write"}, authorization_now_epoch
        )
        timestamp = _timestamp(occurred_at)
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                plan_row = self._plan_row(connection, identity.tenant_id, task_id)
                row = connection.execute(
                    "SELECT * FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                if plan_row is None or row is None:
                    raise WorkflowNotFoundError("Workflow step was not found in the active tenant.")
                if row["status"] != "pending":
                    raise WorkflowConflictError("Only a pending step can start.")
                if row["attempts"] >= row["max_attempts"]:
                    raise WorkflowConflictError("Step retry limit has been reached.")
                dependencies = connection.execute(
                    """
                    SELECT step_id, status FROM cmx_workflow_steps
                    WHERE tenant_id = ? AND task_id = ?
                    """,
                    (identity.tenant_id, task_id),
                ).fetchall()
                plan = ExecutionPlan.model_validate_json(plan_row["plan_json"])
                spec = next((item for item in plan.steps if item.step_id == step_id), None)
                if spec is None:
                    raise WorkflowIntegrityError("Step is absent from the stored immutable plan.")
                dependency_status = {item["step_id"]: item["status"] for item in dependencies}
                if any(dependency_status.get(dep) != "succeeded" for dep in spec.depends_on):
                    raise WorkflowConflictError("A step cannot start until all dependencies have succeeded.")
                if bool(row["requires_approval"]):
                    expected_fingerprint = _approval_fingerprint(
                        tenant_id=identity.tenant_id,
                        task_id=task_id,
                        plan_hash=plan_row["plan_hash"],
                        step=spec,
                        capability_version=row["capability_version"],
                        capability_fingerprint=row["capability_fingerprint"],
                    )
                    if (
                        row["approved_at"] is None
                        or row["approved_by"] is None
                        or row["approval_fingerprint"] is None
                        or not hmac.compare_digest(row["approval_fingerprint"], expected_fingerprint)
                    ):
                        raise StepApprovalRequired("Step requires a valid, input-bound approval before execution.")
                self._write_workflow_state(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    target="running", actor_id=identity.subject_id, occurred_at=timestamp,
                )
                connection.execute(
                    """
                    UPDATE cmx_workflow_steps SET status = 'running', attempts = attempts + 1, updated_at = ?
                    WHERE tenant_id = ? AND task_id = ? AND step_id = ? AND status = 'pending'
                    """,
                    (timestamp, identity.tenant_id, task_id, step_id),
                )
                self._record_step_event(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    actor_id=identity.subject_id, occurred_at=timestamp,
                    event_type="step.started", workflow_state="running", step_id=step_id,
                )
                final = connection.execute(
                    "SELECT * FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._row_to_step(final)

    def complete_step(
        self,
        task_id: str,
        step_id: str,
        output_payload: Any,
        *,
        principal: PrincipalContext | None,
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> WorkflowStepRecord:
        identity = self._authorize(
            principal, {"workflow:execute", "tasks:write"}, authorization_now_epoch
        )
        timestamp = _timestamp(occurred_at)
        output_json = _canonical_json(output_payload)
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                plan_row = self._plan_row(connection, identity.tenant_id, task_id)
                row = connection.execute(
                    "SELECT status FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                if plan_row is None or row is None:
                    raise WorkflowNotFoundError("Workflow step was not found in the active tenant.")
                if row["status"] != "running":
                    raise WorkflowConflictError("Only a running step can complete.")
                connection.execute(
                    """
                    UPDATE cmx_workflow_steps
                    SET status = 'succeeded', output_json = ?, error_code = NULL, updated_at = ?
                    WHERE tenant_id = ? AND task_id = ? AND step_id = ?
                    """,
                    (output_json, timestamp, identity.tenant_id, task_id, step_id),
                )
                self._record_step_event(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    actor_id=identity.subject_id, occurred_at=timestamp,
                    event_type="step.succeeded", workflow_state=plan_row["state"], step_id=step_id,
                )
                final = connection.execute(
                    "SELECT * FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._row_to_step(final)

    def fail_step(
        self,
        task_id: str,
        step_id: str,
        *,
        error_code: str,
        principal: PrincipalContext | None,
        occurred_at: int | None = None,
        allow_pending: bool = False,
        authorization_now_epoch: int | None = None,
    ) -> WorkflowStepRecord:
        identity = self._authorize(
            principal, {"workflow:execute", "tasks:write"}, authorization_now_epoch
        )
        if not _ERROR_CODE_RE.fullmatch(error_code):
            raise ValueError("error_code has an invalid format.")
        timestamp = _timestamp(occurred_at)
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                plan_row = self._plan_row(connection, identity.tenant_id, task_id)
                row = connection.execute(
                    "SELECT status FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                if plan_row is None or row is None:
                    raise WorkflowNotFoundError("Workflow step was not found in the active tenant.")
                permitted = {"running"}
                if allow_pending:
                    permitted.add("pending")
                if row["status"] not in permitted:
                    raise WorkflowConflictError("Step cannot be marked failed from its current state.")
                connection.execute(
                    """
                    UPDATE cmx_workflow_steps
                    SET status = 'failed', error_code = ?, updated_at = ?
                    WHERE tenant_id = ? AND task_id = ? AND step_id = ?
                    """,
                    (error_code, timestamp, identity.tenant_id, task_id, step_id),
                )
                self._write_workflow_state(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    target="failed", actor_id=identity.subject_id, occurred_at=timestamp,
                )
                self._record_step_event(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    actor_id=identity.subject_id, occurred_at=timestamp,
                    event_type="step.failed", workflow_state="failed", step_id=step_id,
                    extra_payload={"error_code": error_code},
                )
                final = connection.execute(
                    "SELECT * FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._row_to_step(final)

    def block_remaining_steps(
        self,
        task_id: str,
        *,
        principal: PrincipalContext | None,
        error_code: str = "dependency_failed",
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> None:
        identity = self._authorize(
            principal, {"workflow:execute", "tasks:write"}, authorization_now_epoch
        )
        if not _ERROR_CODE_RE.fullmatch(error_code):
            raise ValueError("error_code has an invalid format.")
        timestamp = _timestamp(occurred_at)
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_workflow_integrity(connection, identity.tenant_id, task_id)
                rows = connection.execute(
                    """
                    SELECT step_id FROM cmx_workflow_steps
                    WHERE tenant_id = ? AND task_id = ?
                      AND status IN ('pending','awaiting_approval')
                    ORDER BY ordinal
                    """,
                    (identity.tenant_id, task_id),
                ).fetchall()
                plan_row = self._plan_row(connection, identity.tenant_id, task_id)
                if plan_row is None:
                    raise WorkflowNotFoundError("Workflow was not found in the active tenant.")
                for row in rows:
                    connection.execute(
                        """
                        UPDATE cmx_workflow_steps
                        SET status = 'blocked', error_code = ?, updated_at = ?
                        WHERE tenant_id = ? AND task_id = ? AND step_id = ?
                        """,
                        (error_code, timestamp, identity.tenant_id, task_id, row["step_id"]),
                    )
                    self._record_step_event(
                        connection, tenant_id=identity.tenant_id, task_id=task_id,
                        actor_id=identity.subject_id, occurred_at=timestamp,
                        event_type="step.blocked", workflow_state=plan_row["state"],
                        step_id=row["step_id"], extra_payload={"error_code": error_code},
                    )
                if plan_row["state"] not in {"failed", "cancelled", "succeeded"}:
                    self._write_workflow_state(
                        connection, tenant_id=identity.tenant_id, task_id=task_id,
                        target="failed", actor_id=identity.subject_id, occurred_at=timestamp,
                    )
                    self._record_step_event(
                        connection, tenant_id=identity.tenant_id, task_id=task_id,
                        actor_id=identity.subject_id, occurred_at=timestamp,
                        event_type="workflow.state", workflow_state="failed",
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def interrupt_step(
        self,
        task_id: str,
        step_id: str,
        *,
        principal: PrincipalContext | None,
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> WorkflowStepRecord:
        identity = self._authorize(
            principal, {"workflow:execute", "tasks:write"}, authorization_now_epoch
        )
        timestamp = _timestamp(occurred_at)
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                plan_row = self._plan_row(connection, identity.tenant_id, task_id)
                row = connection.execute(
                    "SELECT status FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                if plan_row is None or row is None:
                    raise WorkflowNotFoundError("Workflow step was not found in the active tenant.")
                if row["status"] != "running":
                    raise WorkflowConflictError("Only a running step can be marked interrupted.")
                connection.execute(
                    """
                    UPDATE cmx_workflow_steps SET status = 'interrupted',
                        error_code = 'execution_interrupted', updated_at = ?
                    WHERE tenant_id = ? AND task_id = ? AND step_id = ?
                    """,
                    (timestamp, identity.tenant_id, task_id, step_id),
                )
                self._write_workflow_state(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    target="failed", actor_id=identity.subject_id, occurred_at=timestamp,
                )
                self._record_step_event(
                    connection, tenant_id=identity.tenant_id, task_id=task_id,
                    actor_id=identity.subject_id, occurred_at=timestamp,
                    event_type="step.interrupted", workflow_state="failed", step_id=step_id,
                )
                final = connection.execute(
                    "SELECT * FROM cmx_workflow_steps WHERE tenant_id = ? AND task_id = ? AND step_id = ?",
                    (identity.tenant_id, task_id, step_id),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._row_to_step(final)
