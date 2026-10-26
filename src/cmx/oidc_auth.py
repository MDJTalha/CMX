"""Fail-closed OIDC JWT verification with server-controlled tenant permissions.

This module verifies signed ID tokens/access tokens using issuer JWKS, audience, issuer,
expiry and issued-at claims. Tenant membership and CMX permissions come only from a local
administrative store, never from token-supplied permission claims. It does not implement
browser sessions, login redirects, CSRF protection or an MFA enrollment flow.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager
import time
from pathlib import Path
from typing import Any, Iterable

import jwt
from pydantic import ValidationError

from cmx.security import AuthorizationError, PrincipalContext

_PERMISSION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{1,127}$")
_MFA_METHODS = frozenset({"mfa", "otp", "hwk", "swk", "webauthn"})


class OIDCConfigurationError(ValueError):
    """OIDC verifier configuration is missing or unsafe."""


class OIDCIdentityStore:
    """SQLite store for administrator-managed OIDC tenant membership and permissions."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS cmx_oidc_memberships (
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    permissions_json TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    created_at INTEGER NOT NULL CHECK (created_at >= 0),
                    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
                    PRIMARY KEY (issuer, subject, tenant_id)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _validate_permissions(permissions: Iterable[str]) -> list[str]:
        values = sorted(set(permissions))
        if not values or len(values) > 100 or any(
            not isinstance(item, str) or not _PERMISSION_RE.fullmatch(item) for item in values
        ):
            raise ValueError("At least one valid CMX permission is required.")
        return values

    def grant(
        self, *, issuer: str, subject: str, tenant_id: str,
        permissions: Iterable[str], now_epoch: int | None = None,
    ) -> None:
        now = int(time.time()) if now_epoch is None else now_epoch
        if not isinstance(issuer, str) or not issuer.startswith("https://") or len(issuer) > 500:
            raise ValueError("issuer must be a configured HTTPS OIDC issuer.")
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise ValueError("subject is invalid.")
        if not isinstance(tenant_id, str) or not _TENANT_RE.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid.")
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_epoch must be a non-negative integer.")
        import json
        grants = self._validate_permissions(permissions)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO cmx_oidc_memberships
                       (issuer, subject, tenant_id, permissions_json, active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?)
                       ON CONFLICT(issuer, subject, tenant_id) DO UPDATE SET
                         permissions_json=excluded.permissions_json, active=1, updated_at=excluded.updated_at""",
                    (issuer, subject, tenant_id, json.dumps(grants, separators=(",", ":")), now, now),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def revoke(
        self, *, issuer: str, subject: str, tenant_id: str, now_epoch: int | None = None,
    ) -> bool:
        now = int(time.time()) if now_epoch is None else now_epoch
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE cmx_oidc_memberships SET active=0, updated_at=?
                       WHERE issuer=? AND subject=? AND tenant_id=? AND active=1""",
                    (now, issuer, subject, tenant_id),
                )
                connection.execute("COMMIT")
                return cursor.rowcount == 1
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def get_active_permissions(self, *, issuer: str, subject: str, tenant_id: str) -> frozenset[str] | None:
        import json
        with self._connect() as connection:
            row = connection.execute(
                """SELECT permissions_json FROM cmx_oidc_memberships
                   WHERE issuer=? AND subject=? AND tenant_id=? AND active=1""",
                (issuer, subject, tenant_id),
            ).fetchone()
        if row is None:
            return None
        values = json.loads(row["permissions_json"])
        return frozenset(self._validate_permissions(values))


class OIDCAuthenticator:
    """Verify RS256 bearer JWTs and derive permissions from local membership records."""

    def __init__(
        self, *, issuer: str, audience: str, jwks_url: str,
        identity_store: OIDCIdentityStore, require_mfa: bool = False,
        mfa_acr: str | None = None, leeway_seconds: int = 30,
        jwks_client: Any | None = None,
    ) -> None:
        if not isinstance(issuer, str) or not issuer.startswith("https://") or len(issuer) > 500:
            raise OIDCConfigurationError("issuer must be an HTTPS URL.")
        if not isinstance(audience, str) or not audience.strip() or len(audience) > 512:
            raise OIDCConfigurationError("audience is required.")
        if not isinstance(jwks_url, str) or not jwks_url.startswith("https://") or len(jwks_url) > 2048:
            raise OIDCConfigurationError("jwks_url must be an HTTPS URL.")
        if isinstance(leeway_seconds, bool) or not isinstance(leeway_seconds, int) or not 0 <= leeway_seconds <= 120:
            raise OIDCConfigurationError("leeway_seconds must be between 0 and 120.")
        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self.identity_store = identity_store
        self.require_mfa = require_mfa
        self.mfa_acr = mfa_acr
        self.leeway_seconds = leeway_seconds
        self._jwks_client = jwks_client or jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)

    def authenticate(
        self, token: str, *, tenant_id: str, now_epoch: int | None = None,
    ) -> PrincipalContext:
        now = int(time.time()) if now_epoch is None else now_epoch
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_epoch must be a non-negative integer.")
        if not isinstance(token, str) or not token or len(token) > 16384:
            raise AuthorizationError("invalid_credential", "A valid OIDC bearer token is required.")
        if not isinstance(tenant_id, str) or not _TENANT_RE.fullmatch(tenant_id):
            raise AuthorizationError("invalid_tenant", "A valid tenant selector is required.")
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_seconds,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except Exception as exc:
            raise AuthorizationError("invalid_credential", "OIDC token verification failed.") from exc

        subject = claims.get("sub")
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise AuthorizationError("invalid_credential", "OIDC subject claim is invalid.")
        if isinstance(issued_at, bool) or not isinstance(issued_at, int) or isinstance(expires_at, bool) or not isinstance(expires_at, int):
            raise AuthorizationError("invalid_credential", "OIDC time claims are invalid.")
        if issued_at > now + self.leeway_seconds or expires_at <= now - self.leeway_seconds or expires_at <= issued_at:
            raise AuthorizationError("invalid_credential", "OIDC token is not currently valid.")
        if self.require_mfa:
            amr = claims.get("amr", [])
            acr = claims.get("acr")
            has_mfa = isinstance(amr, list) and any(isinstance(item, str) and item.lower() in _MFA_METHODS for item in amr)
            if not has_mfa and (self.mfa_acr is None or acr != self.mfa_acr):
                raise AuthorizationError("mfa_required", "The OIDC authentication context does not satisfy MFA policy.")

        permissions = self.identity_store.get_active_permissions(
            issuer=self.issuer, subject=subject, tenant_id=tenant_id,
        )
        if permissions is None:
            raise AuthorizationError("membership_required", "No active CMX membership exists for this identity and tenant.")
        subject_id = "oidc_" + hashlib.sha256((self.issuer + "\0" + subject).encode("utf-8")).hexdigest()[:40]
        try:
            return PrincipalContext(
                subject_id=subject_id,
                tenant_id=tenant_id,
                issuer=self.issuer,
                permissions=permissions,
                issued_at=issued_at,
                expires_at=expires_at,
                authentication_verified=True,
                authentication_method="oidc",
                credential_id=str(claims.get("jti"))[:200] if claims.get("jti") else None,
            )
        except ValidationError as exc:
            raise AuthorizationError("invalid_credential", "OIDC identity claims could not form a valid principal.") from exc
