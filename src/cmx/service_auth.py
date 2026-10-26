"""Persistent service credentials for the CMX HTTP trust boundary.

API keys are generated only by trusted local operator tooling. The database stores
a high-entropy secret digest, never the bearer token. This is service-to-service
authentication, not an OIDC, browser-session, password, or MFA implementation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from cmx.security import PrincipalContext

_MAX_CREDENTIAL_LIFETIME_SECONDS = 365 * 24 * 60 * 60
_MIN_CREDENTIAL_LIFETIME_SECONDS = 60
_TOKEN_PATTERN = re.compile(r"^cmx_live_([A-Za-z0-9_-]{12})\.([A-Za-z0-9_-]{43})$")
_ZERO_HASH = "0" * 64


class InvalidServiceCredential(PermissionError):
    """An invalid, expired, not-yet-valid, or revoked service key."""


class CredentialStoreIntegrityError(RuntimeError):
    """The credential audit ledger failed its local integrity check."""


@dataclass(frozen=True)
class IssuedServiceCredential:
    """Credential metadata plus the token, which must only be displayed once."""

    token: str
    credential_id: str
    subject_id: str
    tenant_id: str
    permissions: frozenset[str]
    issued_at: int
    expires_at: int


class SQLiteCredentialStore:
    """File-backed credential registry with one-way revocation and hash-chained audit.

    The SQLite file is trusted local operator state. SQL triggers protect against
    accidental updates/deletes; they are not a defense against an administrator who
    can replace the database or drop its triggers. Protect the file and its backups.
    """

    def __init__(self, database_path: str | Path) -> None:
        raw_path = str(database_path)
        if raw_path == ":memory:":
            raise ValueError("Service credentials require a durable file-backed database.")
        self.database_path = str(Path(raw_path).expanduser())
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cmx_service_credentials (
                    credential_id TEXT PRIMARY KEY,
                    token_prefix TEXT NOT NULL UNIQUE,
                    secret_sha256 TEXT NOT NULL CHECK(length(secret_sha256) = 64),
                    subject_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    issuer TEXT NOT NULL,
                    permissions_json TEXT NOT NULL,
                    issued_at INTEGER NOT NULL CHECK(issued_at >= 0),
                    expires_at INTEGER NOT NULL CHECK(expires_at > issued_at),
                    revoked_at INTEGER CHECK(revoked_at IS NULL OR revoked_at >= issued_at)
                );

                CREATE TABLE IF NOT EXISTS cmx_credential_events (
                    event_id INTEGER PRIMARY KEY,
                    event_type TEXT NOT NULL CHECK(event_type IN ('credential.issued', 'credential.revoked')),
                    credential_id TEXT NOT NULL,
                    occurred_at INTEGER NOT NULL CHECK(occurred_at >= 0),
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL CHECK(length(previous_hash) = 64),
                    event_hash TEXT NOT NULL UNIQUE CHECK(length(event_hash) = 64),
                    FOREIGN KEY(credential_id) REFERENCES cmx_service_credentials(credential_id)
                );

                CREATE TRIGGER IF NOT EXISTS cmx_service_credentials_identity_immutable
                BEFORE UPDATE OF token_prefix, secret_sha256, subject_id, tenant_id, issuer,
                                 permissions_json, issued_at, expires_at
                ON cmx_service_credentials
                BEGIN
                    SELECT RAISE(ABORT, 'service credential identity is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS cmx_service_credentials_no_delete
                BEFORE DELETE ON cmx_service_credentials
                BEGIN
                    SELECT RAISE(ABORT, 'service credentials cannot be deleted; revoke instead');
                END;

                CREATE TRIGGER IF NOT EXISTS cmx_service_credentials_revocation_one_way
                BEFORE UPDATE OF revoked_at ON cmx_service_credentials
                WHEN OLD.revoked_at IS NOT NULL
                     OR NEW.revoked_at IS NULL
                     OR NEW.revoked_at < OLD.issued_at
                BEGIN
                    SELECT RAISE(ABORT, 'service credential revocation is one-way');
                END;

                CREATE TRIGGER IF NOT EXISTS cmx_credential_events_no_update
                BEFORE UPDATE ON cmx_credential_events
                BEGIN
                    SELECT RAISE(ABORT, 'credential audit events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS cmx_credential_events_no_delete
                BEFORE DELETE ON cmx_credential_events
                BEGIN
                    SELECT RAISE(ABORT, 'credential audit events are append-only');
                END;
                """
            )

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @classmethod
    def _hash_event(
        cls,
        *,
        event_id: int,
        event_type: str,
        credential_id: str,
        occurred_at: int,
        payload_json: str,
        previous_hash: str,
    ) -> str:
        material = cls._canonical_json(
            {
                "event_id": event_id,
                "event_type": event_type,
                "credential_id": credential_id,
                "occurred_at": occurred_at,
                "payload_json": payload_json,
                "previous_hash": previous_hash,
            }
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @classmethod
    def _verify_audit_chain(cls, connection: sqlite3.Connection) -> None:
        previous_hash = _ZERO_HASH
        expected_id = 1
        rows = connection.execute(
            "SELECT event_id, event_type, credential_id, occurred_at, payload_json, "
            "previous_hash, event_hash FROM cmx_credential_events ORDER BY event_id"
        ).fetchall()
        for row in rows:
            if row["event_id"] != expected_id or row["previous_hash"] != previous_hash:
                raise CredentialStoreIntegrityError("Credential audit chain failed integrity validation.")
            expected_hash = cls._hash_event(
                event_id=row["event_id"],
                event_type=row["event_type"],
                credential_id=row["credential_id"],
                occurred_at=row["occurred_at"],
                payload_json=row["payload_json"],
                previous_hash=row["previous_hash"],
            )
            if not hmac.compare_digest(expected_hash, row["event_hash"]):
                raise CredentialStoreIntegrityError("Credential audit chain failed integrity validation.")
            try:
                json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise CredentialStoreIntegrityError(
                    "Credential audit chain failed integrity validation."
                ) from exc
            previous_hash = row["event_hash"]
            expected_id += 1

    @classmethod
    def _append_event(
        cls,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        credential_id: str,
        occurred_at: int,
        payload: dict[str, object],
    ) -> None:
        previous = connection.execute(
            "SELECT event_id, event_hash FROM cmx_credential_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        event_id = 1 if previous is None else int(previous["event_id"]) + 1
        previous_hash = _ZERO_HASH if previous is None else str(previous["event_hash"])
        payload_json = cls._canonical_json(payload)
        event_hash = cls._hash_event(
            event_id=event_id,
            event_type=event_type,
            credential_id=credential_id,
            occurred_at=occurred_at,
            payload_json=payload_json,
            previous_hash=previous_hash,
        )
        connection.execute(
            """
            INSERT INTO cmx_credential_events
                (event_id, event_type, credential_id, occurred_at, payload_json,
                 previous_hash, event_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, event_type, credential_id, occurred_at, payload_json,
                previous_hash, event_hash,
            ),
        )

    def verify_audit_chain(self) -> bool:
        """Return true only when every credential event and hash link validates."""
        with self._connect() as connection:
            self._verify_audit_chain(connection)
        return True

    def issue(
        self,
        *,
        subject_id: str,
        tenant_id: str,
        permissions: Iterable[str],
        expires_in_seconds: int,
        now_epoch: int | None = None,
        issuer: str = "urn:cmx:service-key",
    ) -> IssuedServiceCredential:
        """Issue a random bearer key; only the returned value contains its secret."""
        now = int(time.time()) if now_epoch is None else now_epoch
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_epoch must be a non-negative integer.")
        if (
            isinstance(expires_in_seconds, bool)
            or not isinstance(expires_in_seconds, int)
            or not _MIN_CREDENTIAL_LIFETIME_SECONDS <= expires_in_seconds <= _MAX_CREDENTIAL_LIFETIME_SECONDS
        ):
            raise ValueError("Credential lifetime must be between 60 seconds and 365 days.")

        if isinstance(permissions, (str, bytes)):
            raise ValueError("permissions must be a collection of permission names.")
        permission_values = list(permissions)
        if not permission_values or len(permission_values) > 100:
            raise ValueError("At least one and at most 100 permissions must be supplied.")
        if any(not isinstance(item, str) for item in permission_values):
            raise ValueError("Every permission must be a string.")
        if len(set(permission_values)) != len(permission_values):
            raise ValueError("permissions must not contain duplicates.")

        credential_id = "cred_" + secrets.token_hex(16)
        token_prefix = secrets.token_urlsafe(9)
        secret = secrets.token_urlsafe(32)
        token = f"cmx_live_{token_prefix}.{secret}"
        digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
        expires_at = now + expires_in_seconds
        permission_set = frozenset(permission_values)

        # Use the existing internal principal model as the authoritative validation
        # for identity and permission formats before writing any credential state.
        PrincipalContext(
            subject_id=subject_id,
            tenant_id=tenant_id,
            issuer=issuer,
            permissions=permission_set,
            issued_at=now,
            expires_at=expires_at,
            authentication_verified=True,
            authentication_method="service",
            credential_id=credential_id,
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._verify_audit_chain(connection)
                connection.execute(
                    """
                    INSERT INTO cmx_service_credentials
                        (credential_id, token_prefix, secret_sha256, subject_id, tenant_id,
                         issuer, permissions_json, issued_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        credential_id, token_prefix, digest, subject_id, tenant_id,
                        issuer, self._canonical_json(sorted(permission_set)), now, expires_at,
                    ),
                )
                self._append_event(
                    connection,
                    event_type="credential.issued",
                    credential_id=credential_id,
                    occurred_at=now,
                    payload={
                        "subject_id": subject_id,
                        "tenant_id": tenant_id,
                        "token_prefix": token_prefix,
                        "expires_at": expires_at,
                        "permissions": sorted(permission_set),
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return IssuedServiceCredential(
            token=token,
            credential_id=credential_id,
            subject_id=subject_id,
            tenant_id=tenant_id,
            permissions=permission_set,
            issued_at=now,
            expires_at=expires_at,
        )

    def authenticate(
        self,
        token: str,
        *,
        now_epoch: int | None = None,
    ) -> PrincipalContext:
        """Validate a bearer token against server-side records and derive identity."""
        now = int(time.time()) if now_epoch is None else now_epoch
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_epoch must be a non-negative integer.")
        match = _TOKEN_PATTERN.fullmatch(token) if isinstance(token, str) and len(token) <= 128 else None
        if match is None:
            # Perform one digest comparison on malformed credentials as well; do not
            # include credential values or matching details in the public error.
            hmac.compare_digest(hashlib.sha256(b"invalid").hexdigest(), _ZERO_HASH)
            raise InvalidServiceCredential("Invalid service credential.")

        token_prefix, secret = match.groups()
        supplied_digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT credential_id, secret_sha256, subject_id, tenant_id, issuer, "
                "permissions_json, issued_at, expires_at, revoked_at "
                "FROM cmx_service_credentials WHERE token_prefix = ?",
                (token_prefix,),
            ).fetchone()

        if row is None:
            hmac.compare_digest(supplied_digest, _ZERO_HASH)
            raise InvalidServiceCredential("Invalid service credential.")
        if not hmac.compare_digest(supplied_digest, row["secret_sha256"]):
            raise InvalidServiceCredential("Invalid service credential.")
        if row["revoked_at"] is not None or now < row["issued_at"] or now >= row["expires_at"]:
            raise InvalidServiceCredential("Invalid service credential.")

        try:
            permissions = json.loads(row["permissions_json"])
            if not isinstance(permissions, list) or any(not isinstance(item, str) for item in permissions):
                raise ValueError("invalid permissions document")
            return PrincipalContext(
                subject_id=row["subject_id"],
                tenant_id=row["tenant_id"],
                issuer=row["issuer"],
                permissions=frozenset(permissions),
                issued_at=row["issued_at"],
                expires_at=row["expires_at"],
                authentication_verified=True,
                authentication_method="service",
                credential_id=row["credential_id"],
            )
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CredentialStoreIntegrityError(
                "Stored service credential metadata failed validation."
            ) from exc

    def revoke(
        self,
        credential_id: str,
        *,
        now_epoch: int | None = None,
    ) -> bool:
        """Revoke a key once; return false when it is absent or already revoked."""
        now = int(time.time()) if now_epoch is None else now_epoch
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_epoch must be a non-negative integer.")
        if not isinstance(credential_id, str) or not re.fullmatch(r"cred_[0-9a-f]{32}", credential_id):
            raise ValueError("credential_id has an invalid format.")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._verify_audit_chain(connection)
                row = connection.execute(
                    "SELECT subject_id, tenant_id, revoked_at, issued_at "
                    "FROM cmx_service_credentials WHERE credential_id = ?",
                    (credential_id,),
                ).fetchone()
                if row is None or row["revoked_at"] is not None:
                    connection.rollback()
                    return False
                if now < row["issued_at"]:
                    raise ValueError("Revocation time cannot precede credential issuance.")
                connection.execute(
                    "UPDATE cmx_service_credentials SET revoked_at = ? WHERE credential_id = ?",
                    (now, credential_id),
                )
                self._append_event(
                    connection,
                    event_type="credential.revoked",
                    credential_id=credential_id,
                    occurred_at=now,
                    payload={
                        "subject_id": row["subject_id"],
                        "tenant_id": row["tenant_id"],
                    },
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise
