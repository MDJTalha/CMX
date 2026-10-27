# Durable Workflow Scheduler

`SQLiteWorkflowScheduler` provides a persistent, tenant-scoped queue for a single CMX node. It stores enqueue state, attempts, availability time, worker ownership, lease expiry, monotonically increasing fencing tokens and sanitized error codes. Enqueue is idempotent for a tenant/task pair. Workers claim jobs inside `BEGIN IMMEDIATE`; an expired claim can be reclaimed only with a higher fencing token. Stale workers cannot commit a result after a new worker has claimed the job.

A worker calls `run_once(...)` with an already configured `ExecutionCoordinator` and a trusted, upstream-authenticated `PrincipalContext`. The scheduler delegates workflow execution, approval checks, capability validation, step leases and idempotency checks to the coordinator. Completed workflows become succeeded; failed/cancelled workflows become failed; approval-gated workflows become blocked for operator intervention; budget-limited workflows are requeued. Exceptions receive a bounded exponential delay and sanitized error code.

## Limits and operating guidance

- This is a single-node SQLite queue, not a distributed broker, leader election system, or multi-node high-availability guarantee.
- It does not create identities. The supplied principal must be produced by the trusted authentication layer, never by request JSON.
- The scheduler's fencing token protects queue state, while the coordinator's step-level fencing protects workflow transitions. External providers must still enforce their own idempotency keys.
- A process can crash after an external side effect but before the scheduler records completion. Such operations must be reconciled; the queue must not be treated as exactly-once delivery.
- Long-running production workers need a lease-renewal supervisor and a service-managed shutdown protocol. The initial implementation relies on a bounded lease and fencing; it is a reference implementation pending crash-injection and production load testing.
- Failed or approval-blocked jobs are not silently requeued by `enqueue`; an operator must review the workflow and take an explicit action.
