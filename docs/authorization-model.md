# CMX Authorization and Service Credentials

## Implemented HTTP boundary

The assessment evaluation endpoint now requires a server-issued bearer service credential with the exact assessment:write permission. The credential registry is file-backed SQLite and is configured with CMX_AUTH_DB_PATH; the local default is data/cmx-auth.sqlite3. The database path is trusted deployment configuration, not a request parameter.

The API derives the identity principal from a matching server-side record only after it verifies the presented high-entropy secret, issue time, expiry and revocation status. It never accepts subject, tenant, issuer, permission grants, credential ID or an authentication-verified flag from the request body. Public health and product metadata endpoints intentionally remain unauthenticated and return no customer data.

## Issue and revoke credentials

Credential issuance and revocation are available only through the trusted local operator CLI. They are not exposed as public HTTP routes.

After installing the project in the active Python environment:

    python -m pip install -e ".[dev]"

PowerShell example to issue a 30-day key:

    $env:CMX_AUTH_DB_PATH = ".\data\cmx-auth.sqlite3"
    python -m cmx.auth_cli issue --subject-id svc_assessments --tenant-id tenant_alpha --permission assessment:write --expires-in-days 30

The command displays the bearer token once. Save it in a suitable secret manager or protected environment variable. The database stores only the token prefix and SHA-256 digest of the randomly generated 256-bit secret. It does not store the reusable plaintext token. The command also displays the credential ID; keep that ID for revocation.

To revoke an issued key:

    python -m cmx.auth_cli revoke --credential-id cred_REPLACE_WITH_32_HEX_CHARACTERS

Use the exact credential ID printed at issue time. Revocation is one-way; issue a replacement instead of attempting to restore a revoked key. Expiration can be from 60 seconds through 365 days in the store; the CLI permits one through 365 whole days.

The operator who can run the CLI or write the configured auth database can grant tenant permissions. Treat that access as administrative authority. Do not run credential issuance inside a public request handler, commit the database, paste keys into issue trackers, or log authorization headers.

## Stored records and audit

Credential records store a random credential ID, token prefix, secret digest, subject, tenant, issuer, sorted permission set, issue/expiry times and optional revocation time. Credential identity fields cannot be updated or deleted through the normal SQLite table interface; revoked records are retained.

Issuance and revocation are recorded in a separate append-only event table with a SHA-256 hash chain. New issuance and revocation operations verify the existing chain before mutation. The integrity check detects accidental alteration of existing events; it is not a digital signature, an external tamper-proof log, or protection against an administrator replacing the database or disabling its triggers. Backups, filesystem access controls, key rotation, retention and remote log shipping remain operational work.

## Authorization policy

- Missing, malformed, incorrect, future-dated, expired or revoked service keys receive a generic 401 response.
- A valid key without assessment:write receives 403; the response does not disclose the names of missing permissions.
- A storage or integrity failure returns 503 rather than silently accepting a caller.
- Authentication and authorization exceptions do not include the token, secret digest or permission list.
- Assessment input and output remain validated by the existing request/response models.

The evaluation operation is currently stateless. Its tenant identifier is authenticated metadata, not a request-side tenant selection. When assessments become persistent resources, endpoints must resolve the resource's tenant from trusted server-side storage and compare it to the authenticated principal to prevent IDOR/BOLA access.

## Important scope limits

This is service-to-service bearer-key authentication, not a complete human identity platform. No OAuth/OIDC, browser session, password, MFA, user invitation, organization membership workflow or identity-provider integration is implemented. It does not yet provide request rate limits, usage quotas, credential inventory UI, remote audit shipping, a production secrets manager or a production multi-node database. The SQLite file is local operator state and must be protected accordingly.

Do not expose CMX publicly until deployment-specific security review, rate limiting, operational backups, secrets handling, tenant-resource enforcement, and broader HTTP security testing are complete.
