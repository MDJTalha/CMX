# Worker lease reservations

## Current scope

`SQLiteWorkerLeaseStore` provides durable, tenant-scoped reservations for pending workflow steps. SQLite's `BEGIN IMMEDIATE` transaction serializes competing claims on the same database. A claim records a worker ID, expiry and monotonically increasing fencing token. An expired claim can be reassigned; the token increases so stale owners can be rejected by any downstream component that checks it. Renewal and release require the exact current owner and token.

## Important limits

This is a **reservation primitive, not a complete background worker system**. It is not yet wired into `ExecutionCoordinator`; therefore it does not by itself prevent a coordinator from executing a reserved step or make step completion token-aware. Do not use it as a production execution lock until the next integration increment passes fencing tokens through start/complete/fail transitions.

SQLite is a single-node reference backend. `BEGIN IMMEDIATE` provides local database serialization, not a distributed lease service for independently hosted nodes sharing an unreliable network filesystem. Multi-node deployments should use a database and locking model designed for that topology.

Lease expiry is not proof that a previous handler stopped or that an external side effect did not happen. A reclaimed lease must not trigger blind replay. Reconcile external state with a stable idempotency key before retrying side-effecting work.
