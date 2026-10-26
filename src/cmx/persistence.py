"""Tenant-scoped durable task records and tamper-evident audit history.

This is a file-backed SQLite reference implementation. It is suitable for tests and
an initial single-node deployment, not a claim of production-grade multi-node scaling.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from cmx.kernel import StaleTaskVersionError, TaskEvent, TaskRecord, TaskState
from cmx.security import AuthorizationError, PrincipalContext, authorize_permissions

_ZERO_HASH = "0" * 64
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{1,127}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_ALLOWED_STATES_SQL = "'created','planned','running','awaiting_approval','verifying','succeeded','failed','cancelled'"


class TaskNotFoundError(LookupError):
    """No task with this ID is visible in the principal's tenant."""


class TaskAlreadyExistsError(ValueError):
    """A task with the same ID already exists in the active tenant."""


class AuditIntegrityError(RuntimeError):
    """The persisted audit history is incomplete or fails its integrity check."""


class AuditEventRecord(BaseModel):
    """Immutable representation of one persisted task audit event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    tenant_id: str = Field(pattern=_IDENTIFIER_RE.pattern, min_length=2, max_length=128)
    task_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,79}$")
    sequence: int = Field(ge=0)
    event_type: str = Field(pattern=_EVENT_TYPE_RE.pattern, min_length=2, max_length=128)
    occurred_at: int = Field(ge=0)
    actor_id: str = Field(pattern=_IDENTIFIER_RE.pattern, min_length=2, max_length=128)
    payload: dict[str, Any]
    previous_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Audit payload must be finite JSON-compatible data.") from exc


def _event_hash(values: Mapping[str, Any]) -> str:
    """Hash the canonical event content, including its predecessor hash."""
    canonical = _canonical_json(
        {
            "event_id": values["event_id"],
            "tenant_id": values["tenant_id"],
            "task_id": values["task_id"],
            "sequence": values["sequence"],
            "event_type": values["event_type"],
            "occurred_at": values["occurred_at"],
            "actor_id": values["actor_id"],
            "payload": values["payload"],
            "previous_hash": values["previous_hash"],
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timestamp(value: int | None) -> int:
    result = int(time.time()) if value is None else value
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ValueError("Timestamp must be a non-negative integer epoch value.")
    return result


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} has an invalid identifier format.")


class SQLiteTaskStore:
    """SQLite-backed task repository with tenant isolation and append-only audit events.

    Every write uses an IMMEDIATE transaction and validates the expected task version.
    Public methods take a trusted PrincipalContext instead of caller-supplied tenant IDs.
    The principal must be produced by a trusted authentication adapter; this class does
    not verify credentials or accept a context as proof of authentication by itself.
    """

    SCHEMA_VERSION = 1

    def __init__(self, database_path: str | Path, *, timeout_seconds: float = 5.0) -> None:
        raw_path = str(database_path)
        if raw_path == ":memory:":
            raise ValueError(
                "Use a file-backed SQLite path; separate connections are required for durable transactions."
            )
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("timeout_seconds must be greater than 0 and at most 60.")
        self.database_path = str(Path(raw_path))
        self.timeout_seconds = float(timeout_seconds)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than supported version {self.SCHEMA_VERSION}."
                )

            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS cmx_tasks (
                        tenant_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ({_ALLOWED_STATES_SQL})),
                        version INTEGER NOT NULL CHECK (version >= 0),
                        attempts INTEGER NOT NULL CHECK (attempts >= 1),
                        max_attempts INTEGER NOT NULL CHECK (
                            max_attempts >= attempts AND max_attempts <= 5
                        ),
                        last_event TEXT,
                        created_at INTEGER NOT NULL CHECK (created_at >= 0),
                        updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
                        PRIMARY KEY (tenant_id, task_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_cmx_tasks_tenant_updated
                    ON cmx_tasks (tenant_id, updated_at DESC)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cmx_audit_events (
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
                            REFERENCES cmx_tasks (tenant_id, task_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_cmx_audit_task_sequence
                    ON cmx_audit_events (tenant_id, task_id, sequence)
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS cmx_audit_events_no_update
                    BEFORE UPDATE ON cmx_audit_events
                    BEGIN
                        SELECT RAISE(ABORT, 'audit events are immutable');
                    END
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS cmx_audit_events_no_delete
                    BEFORE DELETE ON cmx_audit_events
                    BEGIN
                        SELECT RAISE(ABORT, 'audit events are immutable');
                    END
                    """
                )
                connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _authorize(
        self,
        principal: PrincipalContext | None,
        permission: str,
        authorization_now_epoch: int | None,
    ) -> PrincipalContext:
        if principal is None or not isinstance(principal, PrincipalContext):
            authorize_permissions(
                principal,
                required_permissions={permission},
                resource_tenant_id="",
                now_epoch=authorization_now_epoch,
            )
            raise AuthorizationError("unauthenticated", "A trusted authenticated identity is required.")
        authorize_permissions(
            principal,
            required_permissions={permission},
            resource_tenant_id=principal.tenant_id,
            now_epoch=authorization_now_epoch,
        )
        return principal

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        last_event = TaskEvent(row["last_event"]) if row["last_event"] is not None else None
        return TaskRecord(
            task_id=row["task_id"],
            state=TaskState(row["state"]),
            version=row["version"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            last_event=last_event,
        )

    @staticmethod
    def _row_payload(row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise AuditIntegrityError("Audit history contains invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise AuditIntegrityError("Audit event payload must be a JSON object.")
        return payload

    @classmethod
    def _verify_rows(cls, rows: Sequence[sqlite3.Row]) -> bool:
        """Verify hashes, sequence continuity and legal state transitions."""
        previous = _ZERO_HASH
        previous_payload: dict[str, Any] | None = None
        for sequence, row in enumerate(rows):
            if row["sequence"] != sequence or row["previous_hash"] != previous:
                return False
            try:
                payload = cls._row_payload(row)
                computed = _event_hash(
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
            except (ValueError, AuditIntegrityError):
                return False
            if not hmac.compare_digest(computed, row["event_hash"]):
                return False

            if sequence == 0:
                if (
                    row["event_type"] != "task.created"
                    or payload.get("state") != TaskState.CREATED.value
                    or payload.get("version") != 0
                    or payload.get("attempts") != 1
                    or payload.get("last_event") is not None
                    or not isinstance(payload.get("max_attempts"), int)
                    or not 1 <= payload["max_attempts"] <= 5
                ):
                    return False
            else:
                if previous_payload is None:
                    return False
                if (
                    payload.get("version") != sequence
                    or payload.get("previous_version") != sequence - 1
                    or payload.get("previous_state") != previous_payload.get("state")
                    or payload.get("max_attempts") != previous_payload.get("max_attempts")
                    or payload.get("last_event") != row["event_type"]
                ):
                    return False
                try:
                    previous_task = TaskRecord(
                        task_id=row["task_id"],
                        state=TaskState(previous_payload["state"]),
                        version=previous_payload["version"],
                        attempts=previous_payload["attempts"],
                        max_attempts=previous_payload["max_attempts"],
                        last_event=(
                            TaskEvent(previous_payload["last_event"])
                            if previous_payload.get("last_event") is not None
                            else None
                        ),
                    )
                    expected_task = previous_task.apply(
                        TaskEvent(row["event_type"]),
                        expected_version=previous_task.version,
                    )
                except (KeyError, TypeError, ValueError):
                    return False
                if (
                    payload.get("state") != expected_task.state.value
                    or payload.get("version") != expected_task.version
                    or payload.get("attempts") != expected_task.attempts
                    or payload.get("max_attempts") != expected_task.max_attempts
                    or payload.get("last_event") != expected_task.last_event.value
                ):
                    return False

            previous = row["event_hash"]
            previous_payload = payload
        return bool(rows)

    @classmethod
    def _latest_matches_task(
        cls,
        task_row: sqlite3.Row,
        rows: Sequence[sqlite3.Row],
    ) -> bool:
        if not rows:
            return False
        payload = cls._row_payload(rows[-1])
        return (
            rows[-1]["sequence"] == task_row["version"]
            and payload.get("version") == task_row["version"]
            and payload.get("state") == task_row["state"]
            and payload.get("attempts") == task_row["attempts"]
            and payload.get("max_attempts") == task_row["max_attempts"]
            and payload.get("last_event") == task_row["last_event"]
        )

    @staticmethod
    def _audit_rows(
        connection: sqlite3.Connection,
        tenant_id: str,
        task_id: str,
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT event_id, tenant_id, task_id, sequence, event_type,
                       occurred_at, actor_id, payload_json, previous_hash, event_hash
                FROM cmx_audit_events
                WHERE tenant_id = ? AND task_id = ?
                ORDER BY sequence ASC
                """,
                (tenant_id, task_id),
            ).fetchall()
        )

    @classmethod
    def _append_audit_event(
        cls,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        task_id: str,
        sequence: int,
        event_type: str,
        occurred_at: int,
        actor_id: str,
        payload: dict[str, Any],
    ) -> AuditEventRecord:
        _validate_identifier(tenant_id, "tenant_id")
        _validate_identifier(actor_id, "actor_id")
        if not _EVENT_TYPE_RE.fullmatch(event_type):
            raise ValueError("event_type has an invalid format.")

        existing = cls._audit_rows(connection, tenant_id, task_id)
        if existing and not cls._verify_rows(existing):
            raise AuditIntegrityError("Existing audit history failed its integrity check.")
        if existing and occurred_at < existing[-1]["occurred_at"]:
            raise ValueError("Audit event timestamps must not move backwards.")
        expected_sequence = len(existing)
        if sequence != expected_sequence:
            raise AuditIntegrityError("Audit sequence does not match the current task version.")
        previous_hash = existing[-1]["event_hash"] if existing else _ZERO_HASH
        event_id = str(uuid.uuid4())
        event_payload = dict(payload)
        payload_json = _canonical_json(event_payload)
        fields = {
            "event_id": event_id,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "actor_id": actor_id,
            "payload": event_payload,
            "previous_hash": previous_hash,
        }
        digest = _event_hash(fields)
        connection.execute(
            """
            INSERT INTO cmx_audit_events (
                event_id, tenant_id, task_id, sequence, event_type,
                occurred_at, actor_id, payload_json, previous_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                tenant_id,
                task_id,
                sequence,
                event_type,
                occurred_at,
                actor_id,
                payload_json,
                previous_hash,
                digest,
            ),
        )
        return AuditEventRecord(
            **fields,
            event_hash=digest,
        )

    def create_task(
        self,
        task: TaskRecord,
        *,
        principal: PrincipalContext | None,
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> TaskRecord:
        """Create a fresh task and its sequence-zero audit event atomically."""
        identity = self._authorize(principal, "tasks:create", authorization_now_epoch)
        timestamp = _timestamp(occurred_at)
        if (
            task.state != TaskState.CREATED
            or task.version != 0
            or task.attempts != 1
            or task.last_event is not None
        ):
            raise ValueError("New tasks must start in created state at version 0 and attempt 1.")

        with self._connection() as connection:
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
                        identity.tenant_id,
                        task.task_id,
                        task.state.value,
                        task.version,
                        task.attempts,
                        task.max_attempts,
                        None,
                        timestamp,
                        timestamp,
                    ),
                )
                self._append_audit_event(
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
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                if "cmx_tasks.tenant_id, cmx_tasks.task_id" in str(exc):
                    raise TaskAlreadyExistsError(
                        "A task with this ID already exists in the active tenant."
                    ) from exc
                raise
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return task

    def get_task(
        self,
        task_id: str,
        *,
        principal: PrincipalContext | None,
        authorization_now_epoch: int | None = None,
    ) -> TaskRecord | None:
        """Return a task only within the principal's own tenant."""
        identity = self._authorize(principal, "tasks:read", authorization_now_epoch)
        _validate_identifier(task_id, "task_id")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT task_id, state, version, attempts, max_attempts, last_event
                FROM cmx_tasks WHERE tenant_id = ? AND task_id = ?
                """,
                (identity.tenant_id, task_id),
            ).fetchone()
            if row is None:
                return None
            rows = self._audit_rows(connection, identity.tenant_id, task_id)
        if not self._verify_rows(rows) or not self._latest_matches_task(row, rows):
            raise AuditIntegrityError("Task record does not match its audit history.")
        return self._task_from_row(row)

    def apply_event(
        self,
        task_id: str,
        event: TaskEvent,
        *,
        principal: PrincipalContext | None,
        expected_version: int,
        occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> TaskRecord:
        """Apply one state transition and append its audit record in a single transaction."""
        identity = self._authorize(principal, "tasks:write", authorization_now_epoch)
        _validate_identifier(task_id, "task_id")
        timestamp = _timestamp(occurred_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT task_id, state, version, attempts, max_attempts, last_event
                    FROM cmx_tasks WHERE tenant_id = ? AND task_id = ?
                    """,
                    (identity.tenant_id, task_id),
                ).fetchone()
                if row is None:
                    raise TaskNotFoundError("Task was not found in the active tenant.")
                current_history = self._audit_rows(connection, identity.tenant_id, task_id)
                if not self._verify_rows(current_history) or not self._latest_matches_task(row, current_history):
                    raise AuditIntegrityError("Task record does not match its audit history.")
                current = self._task_from_row(row)
                updated = current.apply(event, expected_version=expected_version)
                cursor = connection.execute(
                    """
                    UPDATE cmx_tasks
                    SET state = ?, version = ?, attempts = ?, max_attempts = ?,
                        last_event = ?, updated_at = ?
                    WHERE tenant_id = ? AND task_id = ? AND version = ?
                    """,
                    (
                        updated.state.value,
                        updated.version,
                        updated.attempts,
                        updated.max_attempts,
                        updated.last_event.value if updated.last_event is not None else None,
                        timestamp,
                        identity.tenant_id,
                        task_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleTaskVersionError("Task version changed during the update.")
                self._append_audit_event(
                    connection,
                    tenant_id=identity.tenant_id,
                    task_id=task_id,
                    sequence=updated.version,
                    event_type=event.value,
                    occurred_at=timestamp,
                    actor_id=identity.subject_id,
                    payload={
                        "state": updated.state.value,
                        "version": updated.version,
                        "previous_version": current.version,
                        "attempts": updated.attempts,
                        "max_attempts": updated.max_attempts,
                        "previous_state": current.state.value,
                        "last_event": updated.last_event.value if updated.last_event is not None else None,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return updated

    def list_audit_events(
        self,
        task_id: str,
        *,
        principal: PrincipalContext | None,
        limit: int = 500,
        authorization_now_epoch: int | None = None,
    ) -> list[AuditEventRecord]:
        """List immutable audit events only for a task in the principal's tenant."""
        identity = self._authorize(principal, "audit:read", authorization_now_epoch)
        _validate_identifier(task_id, "task_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, tenant_id, task_id, sequence, event_type,
                       occurred_at, actor_id, payload_json, previous_hash, event_hash
                FROM cmx_audit_events
                WHERE tenant_id = ? AND task_id = ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (identity.tenant_id, task_id, limit),
            ).fetchall()
        return [
            AuditEventRecord(
                event_id=row["event_id"],
                tenant_id=row["tenant_id"],
                task_id=row["task_id"],
                sequence=row["sequence"],
                event_type=row["event_type"],
                occurred_at=row["occurred_at"],
                actor_id=row["actor_id"],
                payload=json.loads(row["payload_json"]),
                previous_hash=row["previous_hash"],
                event_hash=row["event_hash"],
            )
            for row in rows
        ]

    def verify_audit_chain(
        self,
        task_id: str,
        *,
        principal: PrincipalContext | None,
        authorization_now_epoch: int | None = None,
    ) -> bool:
        """Verify sequence continuity, hashes and agreement with the current task row."""
        identity = self._authorize(principal, "audit:read", authorization_now_epoch)
        _validate_identifier(task_id, "task_id")
        with self._connection() as connection:
            task = connection.execute(
                """
                SELECT state, version, attempts, max_attempts, last_event
                FROM cmx_tasks WHERE tenant_id = ? AND task_id = ?
                """,
                (identity.tenant_id, task_id),
            ).fetchone()
            if task is None:
                return False
            rows = self._audit_rows(connection, identity.tenant_id, task_id)

        return self._verify_rows(rows) and self._latest_matches_task(task, rows)
