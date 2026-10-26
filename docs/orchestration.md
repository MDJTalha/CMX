# CMX Controlled Workflow Orchestration

## Purpose

This increment connects validated execution plans to durable task and workflow records. The coordinator runs dependency-ordered steps through an explicit in-process handler map, validates each capability contract and output, records every step transition, and stops for approval or safe failure.

This is a controlled execution primitive, not a general autonomous agent, distributed queue, or production external-integration platform.

## Execution lifecycle

1. **Submit:** validate the plan DAG, all referenced capabilities, permissions and step input schemas. Pin each capability's semantic version and a SHA-256 fingerprint of its complete declared contract. Persist the task, immutable plan, pending step records, task-creation audit event and workflow-creation audit event.
2. **Start:** move the task through its explicit lifecycle and traverse steps in stable topological order.
3. **Dependencies:** a step runs only after every dependency has a persisted `succeeded` status. Dependency outputs are passed to the handler as data, keyed by step ID.
4. **Approval:** steps marked for approval pause before a handler is invoked. The reviewer must have the `workflow:approve` permission in the same tenant. The approval is bound to the immutable plan hash, step inputs, capability version and full capability-contract fingerprint.
5. **Execute:** only a handler explicitly injected by trusted application code for the exact capability ID can run. There is no dynamic import, tool discovery or arbitrary model-generated executable code.
6. **Validate output:** a handler result must satisfy the capability's declared output JSON Schema before it is stored as successful.
7. **Persist:** each step status/result change and workflow state change appends a hash-chained audit event in the same SQLite transaction as that change.
8. **Complete:** once every step has a schema-valid output, the task enters the verification state and evaluates mandatory step-completion criteria before it can be marked succeeded.

## Handler contract

A handler receives:
- A JSON-safe copy of the step input.
- The JSON-safe outputs of its declared dependencies.
- An immutable execution context with tenant/task/plan/step IDs, capability version, attempt number, timeout budget and a deterministic idempotency key.

The context deliberately does not contain permission grants or credential material. The coordinator passes the declared timeout budget, but a synchronous Python callable cannot be forcibly stopped safely by this module. Any production adapter must enforce its deadline in an appropriate isolation boundary and must not treat timeout cancellation as proof that a side effect did not happen.

No external handlers, LLM providers, vendor APIs, payments or messaging connectors are registered by this repository. An application may inject handler callables; this boundary must be reviewed before registering any side-effecting integration.

## Recovery and retry policy

A step is marked `running` in durable storage before its handler is called. If a later process finds a persisted running step, the result may be uncertain. The coordinator marks it `interrupted`, fails the workflow and does not replay it automatically. This is deliberately conservative: automatic replay of an external side effect could duplicate a purchase, message or other irreversible operation.

This increment does not implement automatic step retries. Per-step attempt limits are stored and enforced at start; a future retry policy must include capability idempotency declarations, reconciliation for uncertain side effects, deadline enforcement and retry/audit tests.

## Integrity and tenant isolation

- Task and workflow records are scoped by the authenticated principal context's tenant.
- The immutable plan JSON is hashed and checked on every read.
- Each step pins its input, capability ID/version/fingerprint and approval requirement.
- Workflow audit events are hash-chained, have contiguous sequence numbers and non-decreasing timestamps.
- Mutable step projections must match the latest corresponding audited projection.
- Step transitions fail closed if persisted workflow data or audit history fails integrity checks.
- The approval record is server-side and audit-logged, but is not a cryptographic signature. The real identity provider/session verifier remains unimplemented.

## Current limitations

- SQLite is a file-backed, single-node reference backend; this is not a distributed worker queue or production multi-node runtime.
- No public workflow API, real credential verifier, session revocation, or durable identity-provider integration is connected.
- No external action adapters are included. A registered handler executes in-process and must enforce its own timeout.
- The success criteria confirm task/step completion and schema conformance; they do not independently prove a real-world business outcome.
- Recovery stops on an uncertain in-flight step rather than retrying automatically.
- Production data-retention, backup/restore, secrets handling, monitoring, and isolation of untrusted handlers remain separate release requirements.
