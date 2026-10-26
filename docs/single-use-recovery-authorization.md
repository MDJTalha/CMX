# Single-use recovery authorization

## Implemented boundary

`SQLiteRetryAuthorizationStore` issues a short-lived, opaque bearer token only when a trusted principal has `workflow:approve`, the provider reconciliation decision is `retry_candidate`, and the durable tenant-scoped idempotency record still matches the operation ID, request hash, and an eligible interrupted state (`dispatching` or `uncertain`). Token lifetime is bounded to 1–300 seconds. Only a SHA-256 token digest is persisted.

Consumption requires a trusted `workflow:execute` principal in the same tenant, a different subject from the approver, and exact matches for operation ID, idempotency key, request hash, and evidence reference. A SQLite `BEGIN IMMEDIATE` transaction validates expiry and atomically marks the ticket consumed before returning. The issue and consume events are hash-chained; audit events are protected against update/delete by SQLite triggers.

## Important limitations

This is authorization infrastructure, not a retry executor. Consuming the token does not run a handler, dispatch a provider call, prove the provider's idempotency semantics, or guarantee exactly-once external effects. A crash after consumption intentionally burns the ticket; an operator must reconcile and issue a new ticket after fresh evidence. There is not yet an atomic transaction spanning token consumption and arbitrary external side effects, so the token must not be treated as proof that a retry completed. The provider adapter remains responsible for authenticating authoritative evidence. Production use still requires OS-level sandboxing, process supervision/resource quotas, a real provider adapter, and end-to-end crash/concurrency testing against the chosen provider.

## API sketch

1. Obtain a fresh `ReconciliationDecision` with `disposition="retry_candidate"`.
2. An authorized reviewer calls `issue(...)`; the raw token is delivered through a secure channel and must not be logged.
3. A distinct authorized executor calls `consume(...)` with the exact operation/evidence binding.
4. Only a separately implemented executor, after rechecking the durable operation and all workflow/approval/lease gates, could consider dispatch. No such executor is implemented by this module.
