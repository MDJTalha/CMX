"""Fail-closed authorization policy for trusted, upstream-verified identities.

This module does not authenticate users or validate tokens. A trusted authentication
adapter must validate credentials and create PrincipalContext only after successful
verification. Never construct this context from client-supplied request JSON.
"""

from __future__ import annotations

import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PERMISSION_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{1,127}$"


class AuthorizationError(PermissionError):
    """A fail-closed authorization denial with a stable, non-sensitive error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PrincipalContext(BaseModel):
    """Trusted identity context passed internally after upstream authentication.

    This is an internal policy input, not an API request model or credential.
    The authentication adapter is responsible for proving identity and for setting
    authentication_verified=True only after verifying the actual credential.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_id: str = Field(pattern=_IDENTIFIER_PATTERN, min_length=2, max_length=128)
    tenant_id: str = Field(pattern=_IDENTIFIER_PATTERN, min_length=2, max_length=128)
    issuer: str = Field(min_length=3, max_length=500)
    permissions: frozenset[str] = Field(default_factory=frozenset, max_length=100)
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=1)
    authentication_verified: bool = False
    authentication_method: Literal["oidc", "session", "service", "test"] = "session"
    credential_id: str | None = Field(default=None, max_length=200)

    @field_validator("permissions")
    @classmethod
    def permission_keys_are_well_formed(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not _PERMISSION_PATTERN.fullmatch(permission) for permission in value):
            raise ValueError("Permission keys must use the documented lowercase permission format.")
        return value

    @model_validator(mode="after")
    def expiry_follows_issue_time(self) -> "PrincipalContext":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at.")
        return self


def authorize_permissions(
    principal: PrincipalContext | None,
    *,
    required_permissions: set[str] | frozenset[str],
    resource_tenant_id: str,
    now_epoch: int | None = None,
) -> None:
    """Authorize a principal for a tenant-scoped operation; deny by default.

    The optional clock argument exists for deterministic tests. Production callers
    should omit it and use the system clock unless a trusted clock is injected.
    """
    now = int(time.time()) if now_epoch is None else now_epoch

    if principal is None or not isinstance(principal, PrincipalContext):
        raise AuthorizationError("unauthenticated", "A trusted authenticated identity is required.")
    if not principal.authentication_verified:
        raise AuthorizationError("unauthenticated", "The identity context has not been verified.")
    if now < principal.issued_at:
        raise AuthorizationError("identity_not_yet_valid", "The identity context is not yet valid.")
    if now >= principal.expires_at:
        raise AuthorizationError("identity_expired", "The identity context has expired.")

    if not isinstance(resource_tenant_id, str) or not resource_tenant_id:
        raise AuthorizationError("invalid_tenant", "A valid resource tenant is required.")
    if principal.tenant_id != resource_tenant_id:
        raise AuthorizationError("tenant_mismatch", "The identity is not authorized for this tenant.")

    if any(not _PERMISSION_PATTERN.fullmatch(item) for item in required_permissions):
        raise AuthorizationError("invalid_policy", "The requested permission policy is invalid.")

    missing = set(required_permissions) - set(principal.permissions)
    if missing:
        raise AuthorizationError("missing_permission", "The identity lacks required permissions.")
