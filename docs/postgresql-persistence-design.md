# PostgreSQL persistence design

## Status

Design record only. PostgreSQL adapters are **not implemented** by this document, and no hosted database is connected. Keep SQLite as the existing local reference implementation until the adapter parity suite passes.

## Why this is a cross-cutting change

CMX currently persists three distinct security/data domains in SQLite:
- Assessment snapshots and evidence claims (SQLiteAssessmentStore).
- Service credentials and their hash-chained audit events (SQLiteCredentialStore).
- OIDC subject-to-tenant permission grants (OIDCIdentityStore).

Moving only assessment records would leave service credentials or OIDC memberships dependent on local files. A deployed instance must use one explicitly configured durable database for every enabled identity/data store, or document and test a deliberate split. Never silently fall back to SQLite in hosted mode.

## Proposed configuration

- CMX_DATABASE_URL: PostgreSQL connection URL, read only by server-side code and never logged.
- When set, all three stores select PostgreSQL implementations.
- When unset, existing SQLite path settings remain available for local development and existing tests.
- A production-like startup must fail closed if PostgreSQL is requested but the driver, URL, TLS connection, or migration is invalid. It must not silently start with empty SQLite files.
- Operator CLI and API must resolve the same store configuration. Issuing a credential into a different database from the API is an operational failure.

Use a maintained PostgreSQL driver (initial candidate: psycopg[binary]) with a pinned compatible version range. Avoid interpolating user-controlled SQL identifiers. Parameter placeholders and transaction behavior must be adapted explicitly; SQLite's question-mark placeholders, BEGIN IMMEDIATE, PRAGMAs, and triggers cannot be copied verbatim.

## Data and invariants

### Assessment snapshots

Preserve the existing tenant-scoped composite key (tenant_id, assessment_id), template version, canonical JSON snapshot, SHA-256 digest, immutable public API behavior, and foreign key from evidence claims to their parent assessment. Every read/list operation must filter by tenant in SQL; never load all tenants and filter in application code.

Use PostgreSQL JSONB only if canonical digest input remains deterministic at the application layer. Store the canonical JSON string used to compute the digest, or reconstruct it from validated data using the existing canonical serializer. Do not hash PostgreSQL's rendered JSON representation.

### Service credentials and audit events

Preserve the token-prefix lookup, one-way secret digest, expiry and revocation checks, permission validation, immutable credential identity, and audit chain verification before credential issuance/revocation. Concurrent event append must serialize safely: lock the audit-head/last event within a transaction (or use a dedicated singleton head row with row-level locking) so two writers cannot commit events with the same predecessor. Event IDs need a concurrency-safe sequence/identity. A database superuser can still alter data; describe the chain as tamper-evident, not tamper-proof.

Do not store or log bearer tokens. Continue displaying the token only once at issuance.

### OIDC memberships

Preserve the unique key (issuer, subject, tenant_id), server-managed permissions, active/revoked semantics, and the existing JWT validation boundary. A token's claims must never grant tenant permissions by themselves. Store configuration and membership changes must be durable across API restarts.

## Implementation sequence

1. Introduce a small store configuration/factory layer without changing the default SQLite behavior.
2. Implement and test PostgreSQL assessment persistence, including snapshot hash validation, evidence provenance, tenant isolation, and duplicate IDs.
3. Implement and test PostgreSQL service credentials with concurrency-safe audit append, revocation, expiry, and chain-tamper detection.
4. Implement and test PostgreSQL OIDC membership persistence and wire cmx-auth to the same factory/configuration as the API.
5. Add optional PostgreSQL integration tests, run against a real PostgreSQL service in CI (not SQLite emulation), including concurrent credential issuance and tenant isolation.
6. Add a deployment configuration only after the host's current no-card signup, Python runtime, outbound database connectivity, TLS, and free-tier persistence limits have been verified.
7. Test deployment restart persistence, credential issuance/authentication, CORS preflight, live search failure behavior, health endpoint, and restore procedure with synthetic data.

## Required release gates

- Existing SQLite suite remains green and backward-compatible.
- PostgreSQL integration suite passes against a real PostgreSQL instance.
- Tenant A cannot read, list, modify, or infer Tenant B's records.
- API and operator CLI demonstrably share the same PostgreSQL database.
- Missing/invalid database configuration fails closed without fallback.
- Secrets are absent from logs, test output, repository history, and frontend bundles.
- Restart/redeploy preserves credentials, revocations, assessments, evidence, and OIDC grants.
- Backup and restore have a rehearsed, documented procedure.
- CI and deployment health checks pass on the exact candidate commit.

Until all gates pass, classify hosted deployment as a disposable demo only; do not store real customer information or describe CMX as production-ready.