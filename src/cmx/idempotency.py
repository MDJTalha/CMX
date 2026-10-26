"""Durable tenant-scoped idempotency ledger for external-operation safety.

The ledger deliberately does not promise exactly-once external effects. A process can
crash after a provider accepts a request and before CMX records the response. Such
operations remain dispatching/uncertain and must not be replayed automatically.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cmx.persistence import SQLiteTaskStore, _canonical_json, _timestamp
from cmx.security import AuthorizationError, PrincipalContext, authorize_permissions

OperationState = Literal["reserved", "dispatching", "succeeded", "uncertain"]
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SCOPE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{1,255}$")
ZERO_HASH = "0" * 64


class IdempotencyConflict(RuntimeError):
    """A key is reused with different input or an unsafe state transition is requested."""


class IdempotencyIntegrityError(RuntimeError):
    """The durable idempotency record or its audit chain is inconsistent."""


class IdempotencyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    tenant_id: str
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_scope: str
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: OperationState
    created_at: int = Field(ge=0)
    updated_at: int = Field(ge=0)
    result: Any = None


class IdempotencyEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    tenant_id: str
    idempotency_key: str
    sequence: int = Field(ge=0)
    event_type: str
    occurred_at: int = Field(ge=0)
    actor_id: str
    payload: dict[str, Any]
    previous_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SQLiteIdempotencyStore:
    """Transactional idempotency records sharing the CMX task database."""

    def __init__(self, task_store: SQLiteTaskStore) -> None:
        self.task_store = task_store
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS cmx_idempotency_operations (
                        tenant_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) = 64),
                        operation_id TEXT NOT NULL UNIQUE,
                        operation_scope TEXT NOT NULL,
                        request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
                        state TEXT NOT NULL CHECK (state IN ('reserved','dispatching','succeeded','uncertain')),
                        result_json TEXT,
                        created_at INTEGER NOT NULL CHECK (created_at >= 0),
                        updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
                        PRIMARY KEY (tenant_id, idempotency_key),
                        CHECK ((state = 'succeeded' AND result_json IS NOT NULL)
                            OR (state != 'succeeded' AND result_json IS NULL))
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS cmx_idempotency_events (
                        event_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        sequence INTEGER NOT NULL CHECK (sequence >= 0),
                        event_type TEXT NOT NULL,
                        occurred_at INTEGER NOT NULL CHECK (occurred_at >= 0),
                        actor_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        previous_hash TEXT NOT NULL CHECK (length(previous_hash) = 64),
                        event_hash TEXT NOT NULL CHECK (length(event_hash) = 64),
                        UNIQUE (tenant_id, idempotency_key, sequence),
                        FOREIGN KEY (tenant_id, idempotency_key)
                            REFERENCES cmx_idempotency_operations (tenant_id, idempotency_key)
                            ON DELETE RESTRICT
                    )"""
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _authorize(self, principal: PrincipalContext | None, *, tenant_id: str, now_epoch: int | None, write: bool) -> None:
        authorize_permissions(
            principal,
            required_permissions={"workflow:execute" if write else "workflow:read"},
            resource_tenant_id=tenant_id,
            now_epoch=now_epoch,
        )

    def _append_event(
        self, connection: sqlite3.Connection, *, tenant_id: str, key: str,
        actor_id: str, event_type: str, occurred_at: int, payload: dict[str, Any],
    ) -> IdempotencyEvent:
        rows = connection.execute(
            """SELECT * FROM cmx_idempotency_events
               WHERE tenant_id = ? AND idempotency_key = ? ORDER BY sequence""",
            (tenant_id, key),
        ).fetchall()
        previous_hash = ZERO_HASH
        sequence = 0
        for row in rows:
            body = {
                "event_id": row["event_id"], "tenant_id": row["tenant_id"],
                "idempotency_key": row["idempotency_key"], "sequence": row["sequence"],
                "event_type": row["event_type"], "occurred_at": row["occurred_at"],
                "actor_id": row["actor_id"], "payload": json.loads(row["payload_json"]),
                "previous_hash": row["previous_hash"],
            }
            expected = _sha256(_canonical_json(body))
            if row["sequence"] != sequence or row["previous_hash"] != previous_hash or not __import__("hmac").compare_digest(row["event_hash"], expected):
                raise IdempotencyIntegrityError("Idempotency audit chain failed integrity verification.")
            previous_hash = row["event_hash"]
            sequence += 1
        event_id = str(uuid.uuid4())
        body = {
            "event_id": event_id, "tenant_id": tenant_id, "idempotency_key": key,
            "sequence": sequence, "event_type": event_type, "occurred_at": occurred_at,
            "actor_id": actor_id, "payload": payload, "previous_hash": previous_hash,
        }
        event_hash = _sha256(_canonical_json(body))
        connection.execute(
            """INSERT INTO cmx_idempotency_events
               (event_id, tenant_id, idempotency_key, sequence, event_type, occurred_at,
                actor_id, payload_json, previous_hash, event_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, tenant_id, key, sequence, event_type, occurred_at, actor_id,
             _canonical_json(payload), previous_hash, event_hash),
        )
        return IdempotencyEvent(
            **body, event_hash=event_hash,
        )

    def _record(self, row: sqlite3.Row) -> IdempotencyRecord:
        return IdempotencyRecord(
            operation_id=row["operation_id"], tenant_id=row["tenant_id"],
            idempotency_key=row["idempotency_key"], operation_scope=row["operation_scope"],
            request_hash=row["request_hash"], state=row["state"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
        )

    def reserve(
        self, operation_scope: str, idempotency_key: str, request_payload: Any, *,
        principal: PrincipalContext, occurred_at: int | None = None,
        authorization_now_epoch: int | None = None,
    ) -> tuple[IdempotencyRecord, bool]:
        """Reserve a key once. The boolean is true only for the newly created reservation."""
        self._authorize(principal, tenant_id=principal.tenant_id, now_epoch=authorization_now_epoch, write=True)
        if not isinstance(operation_scope, str) or not _SCOPE_RE.fullmatch(operation_scope):
            raise ValueError("operation_scope is invalid.")
        if not isinstance(idempotency_key, str) or not _KEY_RE.fullmatch(idempotency_key):
            raise ValueError("idempotency_key must be a lowercase SHA-256 hex digest.")
        request_hash = _sha256(_canonical_json(request_payload))
        timestamp = _timestamp(occurred_at)
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM cmx_idempotency_operations WHERE tenant_id = ? AND idempotency_key = ?",
                    (principal.tenant_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    if row["operation_scope"] != operation_scope or row["request_hash"] != request_hash:
                        raise IdempotencyConflict("Idempotency key was reused with a different operation or payload.")
                    record = self._record(row)
                    connection.execute("COMMIT")
                    return record, False
                operation_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO cmx_idempotency_operations
                       (tenant_id, idempotency_key, operation_id, operation_scope, request_hash,
                        state, result_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'reserved', NULL, ?, ?)""",
                    (principal.tenant_id, idempotency_key, operation_id, operation_scope,
                     request_hash, timestamp, timestamp),
                )
                self._append_event(
                    connection, tenant_id=principal.tenant_id, key=idempotency_key,
                    actor_id=principal.subject_id, event_type="operation.reserved",
                    occurred_at=timestamp, payload={"operation_id": operation_id, "request_hash": request_hash, "operation_scope": operation_scope},
                )
                row = connection.execute(
                    "SELECT * FROM cmx_idempotency_operations WHERE tenant_id = ? AND idempotency_key = ?",
                    (principal.tenant_id, idempotency_key),
                ).fetchone()
                record = self._record(row)
                connection.execute("COMMIT")
                return record, True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _transition(
        self, key: str, target: OperationState, event_type: str, *, principal: PrincipalContext,
        occurred_at: int | None, authorization_now_epoch: int | None, result: Any = None,
    ) -> IdempotencyRecord:
        self._authorize(principal, tenant_id=principal.tenant_id, now_epoch=authorization_now_epoch, write=True)
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise ValueError("idempotency_key must be a lowercase SHA-256 hex digest.")
        timestamp = _timestamp(occurred_at)
        allowed = {"dispatching": {"reserved"}, "succeeded": {"dispatching"}, "uncertain": {"dispatching"}}
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM cmx_idempotency_operations WHERE tenant_id = ? AND idempotency_key = ?",
                    (principal.tenant_id, key),
                ).fetchone()
                if row is None:
                    raise LookupError("Idempotency operation was not found in the active tenant.")
                if row["state"] not in allowed[target]:
                    raise IdempotencyConflict(f"Cannot transition idempotency operation from {row['state']} to {target}.")
                result_json = _canonical_json(result) if target == "succeeded" else None
                connection.execute(
                    """UPDATE cmx_idempotency_operations SET state = ?, result_json = ?, updated_at = ?
                       WHERE tenant_id = ? AND idempotency_key = ? AND state = ?""",
                    (target, result_json, timestamp, principal.tenant_id, key, row["state"]),
                )
                payload = {"operation_id": row["operation_id"], "from_state": row["state"], "to_state": target}
                if target == "succeeded":
                    payload["result_hash"] = _sha256(result_json)
                self._append_event(
                    connection, tenant_id=principal.tenant_id, key=key, actor_id=principal.subject_id,
                    event_type=event_type, occurred_at=timestamp, payload=payload,
                )
                updated = connection.execute(
                    "SELECT * FROM cmx_idempotency_operations WHERE tenant_id = ? AND idempotency_key = ?",
                    (principal.tenant_id, key),
                ).fetchone()
                record = self._record(updated)
                connection.execute("COMMIT")
                return record
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def mark_dispatching(self, key: str, *, principal: PrincipalContext, occurred_at: int | None = None, authorization_now_epoch: int | None = None) -> IdempotencyRecord:
        return self._transition(key, "dispatching", "operation.dispatching", principal=principal, occurred_at=occurred_at, authorization_now_epoch=authorization_now_epoch)

    def _mark_succeeded_in_transaction(
        self, connection: sqlite3.Connection, key: str, result: Any, *,
        principal: PrincipalContext, occurred_at: int,
    ) -> IdempotencyRecord:
        """Commit idempotency success on a caller-owned transaction."""
        row = connection.execute(
            "SELECT * FROM cmx_idempotency_operations WHERE tenant_id = ? AND idempotency_key = ?",
            (principal.tenant_id, key),
        ).fetchone()
        if row is None:
            raise LookupError("Idempotency operation was not found in the active tenant.")
        if row["state"] != "dispatching":
            raise IdempotencyConflict(
                f"Cannot atomically complete idempotency operation from {row['state']}."
            )
        result_json = _canonical_json(result)
        connection.execute(
            """UPDATE cmx_idempotency_operations SET state = 'succeeded', result_json = ?, updated_at = ?
               WHERE tenant_id = ? AND idempotency_key = ? AND state = 'dispatching'""",
            (result_json, occurred_at, principal.tenant_id, key),
        )
        self._append_event(
            connection, tenant_id=principal.tenant_id, key=key,
            actor_id=principal.subject_id, event_type="operation.succeeded",
            occurred_at=occurred_at,
            payload={
                "operation_id": row["operation_id"], "from_state": "dispatching",
                "to_state": "succeeded", "result_hash": _sha256(result_json),
                "atomic_with_workflow_completion": True,
            },
        )
        updated = connection.execute(
            "SELECT * FROM cmx_idempotency_operations WHERE tenant_id = ? AND idempotency_key = ?",
            (principal.tenant_id, key),
        ).fetchone()
        return self._record(updated)

    def mark_succeeded(self, key: str, result: Any, *, principal: PrincipalContext, occurred_at: int | None = None, authorization_now_epoch: int | None = None) -> IdempotencyRecord:
        return self._transition(key, "succeeded", "operation.succeeded", principal=principal, occurred_at=occurred_at, authorization_now_epoch=authorization_now_epoch, result=result)

    def mark_uncertain(self, key: str, *, principal: PrincipalContext, occurred_at: int | None = None, authorization_now_epoch: int | None = None) -> IdempotencyRecord:
        return self._transition(key, "uncertain", "operation.uncertain", principal=principal, occurred_at=occurred_at, authorization_now_epoch=authorization_now_epoch)

    def get(self, key: str, *, principal: PrincipalContext, authorization_now_epoch: int | None = None) -> IdempotencyRecord:
        self._authorize(principal, tenant_id=principal.tenant_id, now_epoch=authorization_now_epoch, write=False)
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise ValueError("idempotency_key must be a lowercase SHA-256 hex digest.")
        with self.task_store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM cmx_idempotency_operations WHERE tenant_id = ? AND idempotency_key = ?",
                (principal.tenant_id, key),
            ).fetchone()
            if row is None:
                raise LookupError("Idempotency operation was not found in the active tenant.")
            return self._record(row)

    def list_events(self, key: str, *, principal: PrincipalContext, authorization_now_epoch: int | None = None) -> tuple[IdempotencyEvent, ...]:
        self._authorize(principal, tenant_id=principal.tenant_id, now_epoch=authorization_now_epoch, write=False)
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise ValueError("idempotency_key must be a lowercase SHA-256 hex digest.")
        with self.task_store._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM cmx_idempotency_events WHERE tenant_id = ? AND idempotency_key = ?
                   ORDER BY sequence""", (principal.tenant_id, key),
            ).fetchall()
            previous_hash = ZERO_HASH
            events = []
            for sequence, row in enumerate(rows):
                payload = json.loads(row["payload_json"])
                body = {
                    "event_id": row["event_id"], "tenant_id": row["tenant_id"],
                    "idempotency_key": row["idempotency_key"], "sequence": row["sequence"],
                    "event_type": row["event_type"], "occurred_at": row["occurred_at"],
                    "actor_id": row["actor_id"], "payload": payload,
                    "previous_hash": row["previous_hash"],
                }
                expected = _sha256(_canonical_json(body))
                if row["sequence"] != sequence or row["previous_hash"] != previous_hash or not __import__("hmac").compare_digest(row["event_hash"], expected):
                    raise IdempotencyIntegrityError("Idempotency audit chain failed integrity verification.")
                events.append(IdempotencyEvent(**body, event_hash=row["event_hash"]))
                previous_hash = row["event_hash"]
            return tuple(events)
