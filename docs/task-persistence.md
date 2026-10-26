# Durable Task Storage and Audit Events

## Scope

`SQLiteTaskStore` is the first file-backed persistence implementation for CMX task records. It is designed as a small, testable reference backend while the task repository contract and production database choice mature.

## Task lifecycle persistence

- Tasks are partitioned by tenant and task identifier.
- Task creation stores the initial state and its sequence-zero audit event in one transaction.
- Every state transition uses an IMMEDIATE SQLite transaction, checks the expected task version, updates state and appends the matching audit event atomically.
- A stale version, illegal state transition or audit write failure rolls the transaction back.
- Reads and transitions derive tenant scope from a trusted internal `PrincipalContext`; clients must never supply a tenant ID as authorization evidence.
- Operations require explicit permissions: `tasks:create`, `tasks:read`, `tasks:write` and `audit:read`.

## Audit event integrity

Each event records:
- Event identifier, tenant and task identifiers.
- Per-task sequence number and event type.
- Occurrence timestamp and actor identifier.
- Structured JSON payload.
- Hash of the previous event and hash of the canonical current event.

The event hash is SHA-256 over a canonical JSON representation with stable key ordering. Database triggers reject ordinary UPDATE and DELETE statements against audit events. On append, existing history is checked before extending the chain. Integrity verification checks sequence continuity, predecessor hashes, each event hash and agreement between the latest event payload and the current task row.

## Guarantees and limits

- **Atomic transition and audit:** the task update and event append commit or roll back together.
- **Optimistic concurrency:** callers must supply the expected version; invalid, stale, non-integer and boolean versions are rejected.
- **Tenant isolation:** lookup keys include the principal's tenant; an absent task and a task in another tenant are indistinguishable to the caller.
- **Tamper evidence:** the hash chain detects partial edits, broken event order and corruption. It is not a digital signature or a defense against a privileged database operator who can disable triggers and rewrite the whole database and hash chain.
- **SQLite scope:** a file-backed, single-node reference backend is not a substitute for a production PostgreSQL deployment strategy, distributed coordination, encrypted backups, retention policy, monitoring or external audit anchoring.
- **Identity boundary:** the store consumes a principal context; it does not verify credentials, sessions, signatures or token revocation.

## Operational requirements before production

Choose and test the production database adapter, define schema migrations, backup/restore procedures, retention and deletion policy, observability, key/secret management and external audit export. Connect a real authentication adapter that derives tenant membership and grants from trusted server-side data. Include concurrency and cross-tenant integration tests at the HTTP boundary before allowing public task execution.
