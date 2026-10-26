# OIDC Authentication Adapter

CMX supports an explicitly configured OIDC bearer-token mode for the API. It validates RS256 JWT signatures against the configured issuer's JWKS, requires issuer/audience/expiry/issued-at/subject claims, checks time bounds, and optionally enforces an MFA claim (`amr` containing a recognized MFA method or an exact configured `acr`). It uses PyJWT's JWKS cache. Do not use arbitrary user-supplied token claims as CMX permissions.

## Configuration

Set these server-side environment variables before starting CMX:

- `CMX_AUTH_MODE=oidc` — explicitly selects OIDC mode. The default remains `service` for backwards compatibility.
- `CMX_OIDC_ISSUER=https://identity.example.com` — exact issuer value from the provider's discovery metadata/token.
- `CMX_OIDC_AUDIENCE=cmx-api` — expected audience registered for this API.
- `CMX_OIDC_JWKS_URL=https://identity.example.com/.well-known/jwks.json` — provider's trusted HTTPS JWKS endpoint.
- `CMX_OIDC_DB_PATH=data/cmx-oidc.sqlite3` — server-side tenant membership/permission database.
- Optional: `CMX_OIDC_REQUIRE_MFA=true` and `CMX_OIDC_MFA_ACR=<exact provider ACR>`.

OIDC requests must include `Authorization: Bearer <signed-token>` and `X-CMX-Tenant: <tenant-id>`. The tenant selector is untrusted input; access is granted only if the verified issuer+subject has an active server-side membership for that tenant. Permission claims in the JWT are ignored.

## Local operator membership administration

Grant only the required permissions, using the same configured issuer and database path:

```powershell
$env:CMX_AUTH_MODE = "oidc"
$env:CMX_OIDC_ISSUER = "https://identity.example.com"
$env:CMX_OIDC_AUDIENCE = "cmx-api"
$env:CMX_OIDC_JWKS_URL = "https://identity.example.com/.well-known/jwks.json"
$env:CMX_OIDC_DB_PATH = "data/cmx-oidc.sqlite3"
cmx-auth oidc-grant --subject "<exact OIDC sub claim>" --tenant-id "tenant_alpha" --permission "assessment:write"
cmx-auth oidc-revoke --subject "<exact OIDC sub claim>" --tenant-id "tenant_alpha"
```

The grant/revoke commands are local administrative operations, not HTTP endpoints. Protect the host and database as privileged security assets. The `--issuer` argument can override `CMX_OIDC_ISSUER` when explicitly supplied.

## Security scope and limitations

- This verifies bearer tokens; it does not implement browser login redirects, server-side browser sessions, session rotation, logout, CSRF protection, account recovery, or MFA enrollment. The optional MFA check trusts only claims in a cryptographically verified token and depends on correct IdP configuration.
- This is not a full OAuth/OIDC conformance certification. Before production, validate provider discovery/JWKS rotation, key rollover, issuer/audience settings, clock synchronization, rate limiting, audit/monitoring, database backup/permissions, and incident response.
- Do not expose local membership administration through public routes. Membership permissions are server-side and must be provisioned out-of-band.
- The API currently uses the selected auth mode for the assessment endpoint. Workflow services still require explicit trusted principal injection; this adapter does not automatically secure every future endpoint.
