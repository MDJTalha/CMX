"""CMX Decision Engine API with a server-side service-credential boundary."""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cmx import __version__
from cmx.engine import evaluate_decision
from cmx.models import DecisionRequest, DecisionResponse
from cmx.security import AuthorizationError, PrincipalContext, authorize_permissions
from cmx.service_auth import (
    CredentialStoreIntegrityError,
    InvalidServiceCredential,
    SQLiteCredentialStore,
)

_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="CMXServiceBearer")
_AUTH_DATABASE_DEFAULT = "data/cmx-auth.sqlite3"


@lru_cache(maxsize=16)
def _credential_store_for_path(database_path: str) -> SQLiteCredentialStore:
    """Initialize each configured local store once per process, not per request."""
    return SQLiteCredentialStore(database_path)


app = FastAPI(
    title="CMX Decision Engine",
    summary="Evidence-aware decision support for Customer & Market Experience.",
    description=(
        "A local-first decision engine that compares supplied candidate records "
        "against explicit requirements. It does not browse the web, validate sources, "
        "or connect to an LLM in this version. Assessment evaluation requires a "
        "server-issued bearer service credential with the assessment:write permission."
    ),
    version=__version__,
)


def require_service_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> PrincipalContext:
    """Authenticate a bearer key using server-side records; never trust client claims."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="A valid CMX service credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    database_path = os.environ.get("CMX_AUTH_DB_PATH", _AUTH_DATABASE_DEFAULT)
    try:
        store = _credential_store_for_path(database_path)
        return store.authenticate(credentials.credentials)
    except InvalidServiceCredential as exc:
        raise HTTPException(
            status_code=401,
            detail="The service credential is invalid, expired, or revoked.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except (CredentialStoreIntegrityError, OSError, sqlite3.Error, ValueError) as exc:
        # Do not expose database paths, metadata, or integrity details in HTTP errors.
        raise HTTPException(
            status_code=503,
            detail="Service authentication is temporarily unavailable.",
        ) from exc


def require_assessment_write(
    principal: PrincipalContext = Depends(require_service_principal),
) -> PrincipalContext:
    """Apply a server-derived permission policy after successful credential checks."""
    try:
        authorize_permissions(
            principal,
            required_permissions=frozenset({"assessment:write"}),
            # Assessment evaluation is stateless at this stage. When assessments
            # become persisted resources, the resource tenant must be read from the
            # server-side record and compared rather than copied from the request.
            resource_tenant_id=principal.tenant_id,
        )
    except AuthorizationError as exc:
        status_code = 403 if exc.code == "missing_permission" else 401
        raise HTTPException(
            status_code=status_code,
            detail="The credential is not authorized for assessment evaluation.",
            headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
        ) from exc
    return principal


@app.get("/health", tags=["Operations"])
def health() -> dict[str, str]:
    """Readiness endpoint; does not claim external dependencies are healthy."""
    return {"status": "ok", "service": "cmx-decision-engine", "version": __version__}


@app.get("/api/v1", tags=["Operations"])
def api_info() -> dict[str, str]:
    return {
        "name": "CMX Decision Engine",
        "version": __version__,
        "brand": "CMX",
        "meaning": "Customer & Market Experience",
        "assessment_authentication": "Bearer service credential",
    }


@app.post(
    "/api/v1/assessments/evaluate",
    response_model=DecisionResponse,
    tags=["Decision Support"],
)
def evaluate_assessment(
    request: DecisionRequest,
    principal: PrincipalContext = Depends(require_assessment_write),
) -> DecisionResponse:
    """Evaluate supplied candidates after authenticating an authorized service."""
    # Referencing the dependency-derived principal makes the trust boundary explicit;
    # the request body contains no identity, permission, or tenant claims.
    _ = principal
    try:
        return evaluate_decision(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def run() -> None:
    """Run locally. Public deployment still needs broader production controls."""
    import uvicorn

    host = os.getenv("CMX_HOST", "127.0.0.1")
    port = int(os.getenv("CMX_PORT", "8000"))
    uvicorn.run("cmx.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
