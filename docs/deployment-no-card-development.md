# No-card development deployment (CMX)

## Status

This is a deployment planning note, not a claim that CMX is deployed. The existing Render Blueprint is intentionally retained unchanged.

A no-card host can be useful for a public demo, but the current API is not safe to deploy there as a persistent multi-tenant service without additional work. The default authentication registry and assessment repository use SQLite files. On an ephemeral filesystem, those files may disappear when an instance is restarted or replaced. Do not use such a deployment for real customer data or issue credentials that must remain valid across restarts.

## Verified repository constraints

- The FastAPI application starts with `uvicorn cmx.main:app --host 0.0.0.0 --port $PORT`.
- The Python package is installed with `pip install .`; the source package is under `src`.
- The existing `render.yaml` requests a Starter web service and a 1 GB persistent disk. This is not a no-card configuration.
- Service credentials are created by the trusted `cmx-auth` operator CLI and stored in the configured authentication database. A token issued against a different local database will not authenticate against a hosted instance.
- Search requires the server-side `CMX_BRAVE_SEARCH_API_KEY` secret. Never place this secret in frontend code or commit it to Git.

## Safe next implementation steps

1. Add a PostgreSQL-backed persistence adapter for both service credentials/audit events and assessment snapshots, with migrations and integration tests.
2. Keep the existing SQLite adapters for local development and backward compatibility.
3. Add environment-based database configuration and test that credential issuance and API verification use the same database.
4. Select a host only after checking its current no-card signup rules, supported Python runtime/container model, outbound network support, and free-tier limits.
5. Deploy a disposable test instance first; verify HTTPS health, authentication and permissions, CORS preflight, live search, tenant isolation, and restart persistence.
6. Only then publish a no-card development deployment configuration. Do not call it production-ready without security, backup, abuse controls, monitoring, and tenant-isolation review.

## Interim recommendation

Until PostgreSQL persistence and deployment tests exist, use the local development environment or a demo with disposable synthetic data only. Do not work around a provider's billing prompt by disabling authentication, exposing an operator endpoint, committing secrets, or relying on ephemeral SQLite for durable data.
