# Idempotency recovery inventory

## Purpose

The durable idempotency ledger can retain operations in `reserved`, `dispatching`, or
`uncertain` after a process interruption. The read-only recovery inventory makes these
records discoverable to an authorized operator without converting discovery into a retry.

## API

Use `SQLiteIdempotencyStore.list_recovery_candidates(principal=..., limit=100)`.
The method requires tenant-scoped `workflow:read`, accepts a bounded limit from 1 to 500,
returns only records owned by the principal's tenant, excludes terminal `succeeded`
operations, and verifies each returned record's hash-chained event history before returning.
Corrupted audit history raises `IdempotencyIntegrityError`; callers must fail closed.

## Operator handling

- `reserved`: the reservation exists, but the durable record alone is not proof of the
  provider's state. Investigate the coordinator timeline before taking action.
- `dispatching`: dispatch intent was persisted. The provider may or may not have applied
  the effect; treat it as unknown.
- `uncertain`: a handler error or validation/timeout boundary left the external outcome
  unknown. Treat it as unknown.
- `succeeded`: excluded from the inventory because the ledger already records success.

This API is diagnostic only. It does not mutate records, authorize retry, consume retry
authorization, invoke handlers, or infer that an external side effect did or did not occur.
Do not reset, delete, or replay a non-terminal record based only on its age or local state.
Resolve uncertain effects through a provider-specific lookup or an evidence-backed operator
review. A provider's idempotency contract and reconciliation adapter must be implemented and
tested before a retry executor can safely replay external operations.

## Limits

The current store is SQLite and intended for single-node coordination. The inventory is
not a distributed scheduler, does not provide provider state, and does not guarantee
exactly-once external effects. A hash chain detects record-level tampering but is not an
external immutable log against a privileged database operator.
