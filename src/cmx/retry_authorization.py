"""Durable, single-use authorization tickets for reviewed recovery operations.

This module is a safety boundary, not a retry executor. It stores only a digest of the
bearer ticket, binds it to a tenant/operation/request/evidence tuple, requires separate
approver and executor identities, and atomically consumes it before any caller can act.
A consumed ticket must never be restored, even after a crash.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cmx.idempotency import IdempotencyRecord, SQLiteIdempotencyStore
from cmx.persistence import SQLiteTaskStore, _canonical_json, _timestamp
from cmx.recovery import ReconciliationDecision
from cmx.security import AuthorizationError, PrincipalContext, authorize_permissions

_ZERO_HASH = "0" * 64


class RetryAuthorizationError(RuntimeError):
    """A recovery ticket is invalid, expired, mismatched, or already consumed."""


class IssuedRetryAuthorization(BaseModel):
    """The raw token is returned only at issue time; never persist or log it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(min_length=32, max_length=128)
    authorization_id: str
    expires_at: int = Field(ge=1)


class ConsumedRetryAuthorization(BaseModel):
    """Auditable authorization metadata returned after atomic consumption."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_id: str
    tenant_id: str
    operation_id: str
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ref: str = Field(min_length=1, max_length=512)
    approver_id: str
    executor_id: str
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=1)
    consumed_at: int = Field(ge=0)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _event_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


class SQLiteRetryAuthorizationStore:
    """SQLite-backed one-time recovery tickets sharing the task database."""

    def __init__(self, task_store: SQLiteTaskStore, idempotency_store: SQLiteIdempotencyStore) -> None:
        self.task_store = task_store
        self.idempotency_store = idempotency_store
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS cmx_retry_authorizations (
                        authorization_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        evidence_ref TEXT NOT NULL,
                        token_hash TEXT NOT NULL UNIQUE,
                        approver_id TEXT NOT NULL,
                        issued_at INTEGER NOT NULL CHECK (issued_at >= 0),
                        expires_at INTEGER NOT NULL CHECK (expires_at > issued_at),
                        consumed_at INTEGER,
                        executor_id TEXT,
                        FOREIGN KEY (tenant_id, idempotency_key)
                            REFERENCES cmx_idempotency_operations (tenant_id, idempotency_key)
                            ON DELETE RESTRICT,
                        CHECK ((consumed_at IS NULL AND executor_id IS NULL) OR
                               (consumed_at IS NOT NULL AND executor_id IS NOT NULL))
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS cmx_retry_authorization_events (
                        event_id TEXT PRIMARY KEY,
                        authorization_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL CHECK (sequence >= 0),
                        event_type TEXT NOT NULL,
                        occurred_at INTEGER NOT NULL CHECK (occurred_at >= 0),
                        actor_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        previous_hash TEXT NOT NULL CHECK (length(previous_hash) = 64),
                        event_hash TEXT NOT NULL CHECK (length(event_hash) = 64),
                        UNIQUE (authorization_id, sequence),
                        FOREIGN KEY (authorization_id)
                            REFERENCES cmx_retry_authorizations (authorization_id) ON DELETE RESTRICT
                    )"""
                )
                connection.execute(
                    """CREATE TRIGGER IF NOT EXISTS cmx_retry_auth_events_no_update
                       BEFORE UPDATE ON cmx_retry_authorization_events
                       BEGIN SELECT RAISE(ABORT, 'authorization events are immutable'); END"""
                )
                connection.execute(
                    """CREATE TRIGGER IF NOT EXISTS cmx_retry_auth_events_no_delete
                       BEFORE DELETE ON cmx_retry_authorization_events
                       BEGIN SELECT RAISE(ABORT, 'authorization events are immutable'); END"""
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _authorize(principal: PrincipalContext | None, permission: str, now: int) -> PrincipalContext:
        if principal is None or not isinstance(principal, PrincipalContext):
            authorize_permissions(principal, required_permissions={permission}, resource_tenant_id="", now_epoch=now)
            raise AuthorizationError("unauthenticated", "A trusted authenticated identity is required.")
        authorize_permissions(
            principal, required_permissions={permission}, resource_tenant_id=principal.tenant_id, now_epoch=now
        )
        return principal

    def _append_event(
        self, connection: sqlite3.Connection, *, authorization_id: str, actor_id: str,
        event_type: str, occurred_at: int, payload: dict[str, Any],
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM cmx_retry_authorization_events WHERE authorization_id=? ORDER BY sequence",
            (authorization_id,),
        ).fetchall()
        previous = _ZERO_HASH
        sequence = 0
        for row in rows:
            body = {
                "event_id": row["event_id"], "authorization_id": row["authorization_id"],
                "sequence": row["sequence"], "event_type": row["event_type"],
                "occurred_at": row["occurred_at"], "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]), "previous_hash": row["previous_hash"],
            }
            if row["sequence"] != sequence or row["previous_hash"] != previous or row["event_hash"] != _event_hash(body):
                raise RetryAuthorizationError("Recovery authorization audit chain integrity check failed.")
            previous = row["event_hash"]
            sequence += 1
        event_id = str(uuid.uuid4())
        body = {
            "event_id": event_id, "authorization_id": authorization_id, "sequence": sequence,
            "event_type": event_type, "occurred_at": occurred_at, "actor_id": actor_id,
            "payload": payload, "previous_hash": previous,
        }
        connection.execute(
            """INSERT INTO cmx_retry_authorization_events
               (event_id, authorization_id, sequence, event_type, occurred_at, actor_id,
                payload_json, previous_hash, event_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, authorization_id, sequence, event_type, occurred_at, actor_id,
             _canonical_json(payload), previous, _event_hash(body)),
        )

    def issue(
        self, record: IdempotencyRecord, decision: ReconciliationDecision, *,
        principal: PrincipalContext, now_epoch: int | None = None, ttl_seconds: int = 120,
    ) -> IssuedRetryAuthorization:
        now = _timestamp(now_epoch)
        approver = self._authorize(principal, "workflow:approve", now)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 300:
            raise ValueError("ttl_seconds must be an integer between 1 and 300 seconds.")
        if decision.disposition != "retry_candidate" or decision.safe_to_replay is not False:
            raise RetryAuthorizationError("Only a fail-closed retry-candidate decision may be authorized.")
        if not decision.evidence_ref or len(decision.evidence_ref) > 512:
            raise RetryAuthorizationError("A bounded evidence reference is required.")
        durable = self.idempotency_store.get(
            record.idempotency_key, principal=approver, authorization_now_epoch=now
        )
        if (durable.operation_id != record.operation_id or durable.request_hash != record.request_hash
                or durable.tenant_id != approver.tenant_id or durable.state not in {"dispatching", "uncertain"}):
            raise RetryAuthorizationError("The durable operation binding or state does not permit recovery authorization.")
        token = secrets.token_urlsafe(32)
        authorization_id = str(uuid.uuid4())
        expires_at = now + ttl_seconds
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO cmx_retry_authorizations
                       (authorization_id, tenant_id, operation_id, idempotency_key, request_hash,
                        evidence_ref, token_hash, approver_id, issued_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (authorization_id, approver.tenant_id, durable.operation_id, durable.idempotency_key,
                     durable.request_hash, decision.evidence_ref, _digest(token), approver.subject_id, now, expires_at),
                )
                self._append_event(
                    connection, authorization_id=authorization_id, actor_id=approver.subject_id,
                    event_type="retry_authorization.issued", occurred_at=now,
                    payload={"tenant_id": approver.tenant_id, "operation_id": durable.operation_id,
                             "idempotency_key": durable.idempotency_key, "request_hash": durable.request_hash,
                             "evidence_ref": decision.evidence_ref, "expires_at": expires_at},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return IssuedRetryAuthorization(token=token, authorization_id=authorization_id, expires_at=expires_at)

    def consume(
        self, token: str, *, operation_id: str, idempotency_key: str, request_hash: str,
        evidence_ref: str, principal: PrincipalContext, now_epoch: int | None = None,
    ) -> ConsumedRetryAuthorization:
        now = _timestamp(now_epoch)
        executor = self._authorize(principal, "workflow:execute", now)
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise RetryAuthorizationError("Recovery authorization token is invalid.")
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 256:
            raise ValueError("operation_id is invalid.")
        for value, label in ((idempotency_key, "idempotency_key"), (request_hash, "request_hash")):
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest.")
        if not isinstance(evidence_ref, str) or not evidence_ref or len(evidence_ref) > 512:
            raise ValueError("evidence_ref is invalid.")
        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM cmx_retry_authorizations WHERE tenant_id=? AND token_hash=?",
                    (executor.tenant_id, _digest(token)),
                ).fetchone()
                if row is None:
                    raise RetryAuthorizationError("Recovery authorization was not found in this tenant.")
                if row["consumed_at"] is not None:
                    raise RetryAuthorizationError("Recovery authorization has already been consumed.")
                if now >= row["expires_at"]:
                    raise RetryAuthorizationError("Recovery authorization has expired.")
                if row["approver_id"] == executor.subject_id:
                    raise RetryAuthorizationError("The approver and executor must be different identities.")
                if (row["operation_id"] != operation_id or row["idempotency_key"] != idempotency_key
                        or row["request_hash"] != request_hash or row["evidence_ref"] != evidence_ref):
                    raise RetryAuthorizationError("Recovery authorization binding does not match this operation and evidence.")
                # Revalidate the operation in the same transaction that consumes the ticket.
                # An approval issued while the operation was uncertain must not remain usable
                # after another actor resolves it or changes its durable binding.
                durable = connection.execute(
                    """SELECT operation_id, request_hash, state FROM cmx_idempotency_operations
                       WHERE tenant_id=? AND idempotency_key=?""",
                    (executor.tenant_id, idempotency_key),
                ).fetchone()
                if (durable is None or durable["operation_id"] != operation_id
                        or durable["request_hash"] != request_hash
                        or durable["state"] not in {"dispatching", "uncertain"}):
                    raise RetryAuthorizationError(
                        "The durable operation changed or is no longer eligible for recovery."
                    )
                updated = connection.execute(
                    """UPDATE cmx_retry_authorizations SET consumed_at=?, executor_id=?
                       WHERE authorization_id=? AND consumed_at IS NULL AND expires_at>?""",
                    (now, executor.subject_id, row["authorization_id"], now),
                )
                if updated.rowcount != 1:
                    raise RetryAuthorizationError("Recovery authorization was concurrently consumed or expired.")
                self._append_event(
                    connection, authorization_id=row["authorization_id"], actor_id=executor.subject_id,
                    event_type="retry_authorization.consumed", occurred_at=now,
                    payload={"operation_id": operation_id, "idempotency_key": idempotency_key,
                             "request_hash": request_hash, "evidence_ref": evidence_ref},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return ConsumedRetryAuthorization(
            authorization_id=row["authorization_id"], tenant_id=row["tenant_id"],
            operation_id=row["operation_id"], idempotency_key=row["idempotency_key"],
            request_hash=row["request_hash"], evidence_ref=row["evidence_ref"],
            approver_id=row["approver_id"], executor_id=executor.subject_id,
            issued_at=row["issued_at"], expires_at=row["expires_at"], consumed_at=now,
        )

    def consume_and_claim_recovery(
        self, token: str, *, operation_id: str, idempotency_key: str,
        request_hash: str, evidence_ref: str, principal: PrincipalContext,
        now_epoch: int | None = None,
    ) -> tuple[ConsumedRetryAuthorization, IdempotencyRecord]:
        """Atomically consume a reviewed ticket and claim an uncertain operation.

        This is a durable claim, not a provider dispatch. The single SQLite
        transaction prevents ticket consumption without the matching state
        transition and prevents two workers from claiming the same operation.
        """
        now = _timestamp(now_epoch)
        executor = self._authorize(principal, "workflow:execute", now)
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise RetryAuthorizationError("Recovery authorization token is invalid.")
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 256:
            raise ValueError("operation_id is invalid.")
        for value, label in ((idempotency_key, "idempotency_key"), (request_hash, "request_hash")):
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest.")
        if not isinstance(evidence_ref, str) or not evidence_ref or len(evidence_ref) > 512:
            raise ValueError("evidence_ref is invalid.")

        with self.task_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                ticket = connection.execute(
                    "SELECT * FROM cmx_retry_authorizations WHERE tenant_id=? AND token_hash=?",
                    (executor.tenant_id, _digest(token)),
                ).fetchone()
                if ticket is None:
                    raise RetryAuthorizationError("Recovery authorization was not found in this tenant.")
                if ticket["consumed_at"] is not None:
                    raise RetryAuthorizationError("Recovery authorization has already been consumed.")
                if now >= ticket["expires_at"]:
                    raise RetryAuthorizationError("Recovery authorization has expired.")
                if ticket["approver_id"] == executor.subject_id:
                    raise RetryAuthorizationError("The approver and executor must be different identities.")
                if (ticket["operation_id"] != operation_id
                        or ticket["idempotency_key"] != idempotency_key
                        or ticket["request_hash"] != request_hash
                        or ticket["evidence_ref"] != evidence_ref):
                    raise RetryAuthorizationError("Recovery authorization binding does not match this operation and evidence.")

                durable = connection.execute(
                    """SELECT * FROM cmx_idempotency_operations
                       WHERE tenant_id=? AND idempotency_key=?""",
                    (executor.tenant_id, idempotency_key),
                ).fetchone()
                if (durable is None or durable["operation_id"] != operation_id
                        or durable["request_hash"] != request_hash or durable["state"] != "uncertain"):
                    raise RetryAuthorizationError(
                        "Recovery claim requires the matching durable operation to be uncertain."
                    )

                updated = connection.execute(
                    """UPDATE cmx_retry_authorizations SET consumed_at=?, executor_id=?
                       WHERE authorization_id=? AND consumed_at IS NULL AND expires_at>?""",
                    (now, executor.subject_id, ticket["authorization_id"], now),
                )
                if updated.rowcount != 1:
                    raise RetryAuthorizationError("Recovery authorization was concurrently consumed or expired.")

                changed = connection.execute(
                    """UPDATE cmx_idempotency_operations
                       SET state='dispatching', result_json=NULL, updated_at=?
                       WHERE tenant_id=? AND idempotency_key=? AND operation_id=?
                         AND request_hash=? AND state='uncertain'""",
                    (now, executor.tenant_id, idempotency_key, operation_id, request_hash),
                )
                if changed.rowcount != 1:
                    raise RetryAuthorizationError("Recovery operation was concurrently claimed.")

                self._append_event(
                    connection, authorization_id=ticket["authorization_id"],
                    actor_id=executor.subject_id, event_type="retry_authorization.consumed",
                    occurred_at=now,
                    payload={"operation_id": operation_id, "idempotency_key": idempotency_key,
                             "request_hash": request_hash, "evidence_ref": evidence_ref,
                             "claim_state": "dispatching"},
                )
                self.idempotency_store._append_event(
                    connection, tenant_id=executor.tenant_id, key=idempotency_key,
                    actor_id=executor.subject_id, event_type="operation.recovery_dispatch_claimed",
                    occurred_at=now,
                    payload={"operation_id": operation_id, "from_state": "uncertain",
                             "to_state": "dispatching", "authorization_id": ticket["authorization_id"],
                             "evidence_ref": evidence_ref},
                )
                updated_row = connection.execute(
                    """SELECT * FROM cmx_idempotency_operations
                       WHERE tenant_id=? AND idempotency_key=?""",
                    (executor.tenant_id, idempotency_key),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

        consumed = ConsumedRetryAuthorization(
            authorization_id=ticket["authorization_id"], tenant_id=ticket["tenant_id"],
            operation_id=ticket["operation_id"], idempotency_key=ticket["idempotency_key"],
            request_hash=ticket["request_hash"], evidence_ref=ticket["evidence_ref"],
            approver_id=ticket["approver_id"], executor_id=executor.subject_id,
            issued_at=ticket["issued_at"], expires_at=ticket["expires_at"], consumed_at=now,
        )
        return consumed, self.idempotency_store._record(updated_row)

