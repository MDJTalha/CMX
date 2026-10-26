# CMX Reasoning Kernel — State and Capability Contracts

## Purpose

This increment adds deterministic contracts for task lifecycle management, bounded plans, and capability authorization. It is a foundation for a future orchestrator, not an autonomous agent and not a tool-execution runtime.

## Task state machine

Allowed states are:

- `created`
- `planned`
- `running`
- `awaiting_approval`
- `verifying`
- `succeeded`
- `failed`
- `cancelled`

State changes require an explicit event. Terminal states cannot be reopened. A task can reach `succeeded` only after entering `verifying` and receiving `verification_passed`. Approval rejection cancels a waiting task. Retries are bounded by `max_attempts`.

`TaskRecord.apply` requires the caller's expected version. This is an optimistic-concurrency guard for future persistent storage; the current in-memory model does not itself provide database transaction isolation.

## Execution plans

A plan contains between 1 and 30 steps. Each step has a stable identifier, declared capability, objective, dependencies, structured input, approval requirement, and bounded attempt count.

Plan validation rejects duplicate IDs, unknown dependencies, self-dependencies and cycles before execution. The current kernel does not schedule, run, or persist plans.

## Capability registry

Each capability declares:

- Stable ID and semantic version.
- Input and output JSON-schema documents.
- Required permission strings.
- Maximum timeout and retry count.
- Enabled/disabled status.
- Whether it has side effects and whether human approval is required.

The registry is an explicit allowlist. Unknown and disabled capabilities fail closed. Authorization requires every declared permission. Side-effecting capabilities must require approval, and plans must explicitly mark affected steps for approval. Plan validation also prevents a step from exceeding the capability's retry allowance.

## Security boundaries and limitations

- Input and output payloads can now be checked against declared JSON Schemas, including plan-step inputs before execution. This still does not invoke an executor or prove that a real-world side effect occurred.
- A permission set passed by a caller is not authentication. Production must derive permissions from a trusted identity and authorization service.
- Human approval is represented as a required workflow condition, not a UI, cryptographic approval record or notification system.
- No LLM, web retrieval, ML model, neural network, quantum-computing service, database, authentication provider or external action is connected.
- Do not expose the service publicly until authentication, tenant isolation, authorization, rate limits, audit logging, secrets handling, privacy controls and security testing are implemented.

## Verification approach

Automated tests cover legal and illegal state transitions, stale versions, terminal-state behavior, bounded retries, approval rejection, duplicate capabilities, disabled/unknown capabilities, permission enforcement, invalid dependency graphs and approval/retry-policy enforcement.

Verification contracts are now included. Each contract must define at least one mandatory criterion; duplicate or unexpected results are rejected. A failed mandatory criterion yields failed verification, while missing/unknown mandatory evidence yields blocked verification. Only passing every mandatory criterion yields a passed result. This is a deterministic contract evaluator, not an independent external verifier: evidence quality still depends on the trusted caller.

Future increments should add trusted identity-bound permissions, persistent task storage and audit records, cancellation semantics for in-flight work, an executor interface with timeout/idempotency boundaries, and end-to-end integration tests before enabling any real executor.
