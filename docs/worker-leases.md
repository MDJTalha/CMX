# Worker lease reservations

## Current scope

`SQLiteWorkerLeaseStore` provides durable, tenant-scoped reservations for pending workflow steps. SQLite's `BEGIN IMMEDIATE` serializes competing claims on the same database. Each claim records a worker ID, expiry and monotonically increasing fencing token. Renewal and release require the exact current owner and token. The coordinator claims before starting a step, passes the token to state transitions, and renews the lease in a background heartbeat while the synchronous handler runs.

## Safety behavior

- A second coordinator leaves a running step alone while its lease is active.
- If lease renewal fails, the coordinator does not commit a handler result. The running step remains an uncertain outcome and requires deliberate reconciliation.
- A missing or expired lease never causes automatic replay of a step that was already running; external side effects may have happened.
- Capability handlers/adapters must enforce their declared deadline. The heartbeat keeps the reservation alive but does not cancel or sandbox an in-process handler.
- The lease duration is bounded to 30–3600 seconds and is derived from the capability timeout plus a safety margin. Heartbeat renewal uses the same fencing token.

## Important limits

This is still a single-node SQLite coordination design, not a distributed queue. SQLite's local write serialization is not a distributed lease service for independent nodes or unreliable network filesystems. Background scheduling, process-level cancellation, OS sandboxing, resource quotas, and safe reconciliation of external side effects remain open work. Do not blindly replay uncertain operations; use stable idempotency keys and reconcile external state first.
