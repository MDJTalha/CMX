# CMX Roadmap

Roadmap items are proposals, not implemented capabilities. Progress is gated on tests, customer validation and security review.

## Phase 0 — Decision core

- [x] Structured problem, requirement and candidate contracts
- [x] Deterministic capability constraints
- [x] Explicit unknown and excluded states
- [x] First-year cost evaluation with cautious handling of unverified prices
- [x] Explainable weighted requirement coverage
- [x] FastAPI endpoints and automated tests
- [ ] Add dependency security scanning and coverage thresholds to CI

## Phase 1 — Validation and persistence

- [ ] Validate one SME workflow with customer interviews and paid pilots
- [ ] Add versioned assessment templates
- [ ] Add database persistence and immutable decision/audit identifiers
- [ ] Add claim-level evidence records and expiry/freshness policies
- [ ] Add schema migration and backup/restore tests

## Phase 2 — Core reasoning kernel

- [x] Explicit task state machine and bounded task lifecycle contracts
- [x] Capability registry with input/output schema metadata, permissions and timeouts
- [x] Plan representation with dependency validation and retry limits
- [x] Independent verification contracts and testable completion criteria
- [x] Deterministic core remains usable without an LLM
- [x] Add fail-closed tenant-scoped authorization policy against an internally supplied principal context
- [x] Verify server-issued service bearer credentials against hashed server-side records, including expiry and one-way revocation for assessment evaluation
- [ ] Integrate human identity through OIDC or equivalent and implement browser sessions/MFA where needed
- [x] Validate capability schemas and plan inputs against bounded JSON Schema Draft 2020-12 contracts
- [x] Add a controlled coordinator for explicitly injected capability handlers and topologically ordered plans
- [x] Persist immutable workflow plans, step states/results, approvals and hash-chained workflow audit events
- [x] Bind approval records to plan hash, inputs and capability-contract fingerprint
- [x] Add an opt-in fixed-command subprocess adapter with parent-enforced deadlines, bounded JSON streams and explicit unknown-outcome errors
- [ ] Add OS-level sandbox/descendant-process supervision and resource quotas; provider-specific adapters and safe retry execution remain unimplemented
- [x] Add a tenant-scoped durable idempotency ledger with canonical request binding, hash-chained transition events, duplicate-key conflict detection, and fail-closed reservation/dispatch/success/uncertain states; coordinator refuses to invoke a handler when a prior record exists
- [x] Add a provider-neutral reconciliation observation contract with strict operation/key binding, freshness checks, and fail-closed decisions; it does not implement a real provider adapter or authorize replay
- [ ] Add background worker scheduling and full recovery/reconciliation for uncertain outcomes (evidence-backed operator reviews, a read-only uncertain-step inventory, and schema-validated success-resolution preflight are implemented; atomic terminal resolutions are now implemented for reviewed non-application and externally confirmed success cases; both update the interrupted step and append an audit event in one SQLite transaction, while keeping the workflow failed and never replaying handlers; resuming downstream execution and a full retry/idempotency policy remain open)
- [x] Bound handler executions per coordinator invocation and allow safe resume on a subsequent call
- [x] Renew active worker leases during handler execution and reject result commits after heartbeat loss
- [x] Add SQLite-backed worker reservation records with expiry and monotonically increasing fencing tokens
- [x] Enforce lease fencing in coordinator start/complete/failure transitions and avoid interrupting steps with live leases
- [x] Add file-backed SQLite task persistence, optimistic version checks and transactional audit append tests

## Phase 3 — Evidence retrieval and model adapters

- [ ] Pluggable LLM provider interface with no provider lock-in
- [ ] Retrieval over approved documents and curated vendor records
- [ ] Source provenance, retrieval timestamps and freshness checks
- [ ] Prompt-injection and untrusted-content tests
- [ ] Evaluation set measuring factual support, constraint adherence and cost

## Phase 4 — Secure organizational product

- [x] Enforce a server-derived permission on the assessment evaluation HTTP endpoint
- [ ] Add resource-level tenant enforcement as data-backed endpoints are introduced
- [ ] Add OAuth/OIDC, human sessions, MFA and managed organization membership where required
- [ ] Add secrets management, rate limits, quotas and abuse protections
- [ ] Add privacy/data retention controls and deletion workflows
- [ ] Complete security review, backups, monitoring, incident response and production readiness checks

## Phase 5 — Commercial services

- [ ] Vendor-neutral implementation playbooks
- [ ] Baseline and outcome measurement
- [ ] Customer workspace and subscription billing
- [ ] Vendor portal and consent-based qualified leads
- [ ] Clearly labelled sponsored discovery kept separate from organic ranking

## Phase 6 — Advanced research

- [ ] Evaluate ML only when adequate task/outcome data exists
- [ ] Benchmark specialist neural models only for validated use cases
- [ ] Investigate quantum-inspired/quantum methods only against classical baselines

## Current increment

The coordinator now has the foundations for bounded execution and explicit worker lease fencing. This increment adds privileged, tenant-scoped reconciliation reviews for interrupted steps and a read-only inventory that exposes lease state and the latest review. Reviewers must provide a decision, evidence reference and notes; reviews are appended to the hash-chained workflow audit log. A follow-up hardening change uses the trusted authorization clock for lease checks, never caller-supplied audit timestamps. A success-resolution preflight now requires the latest review to confirm the external effect, rejects live leases and changed capability contracts, and validates the proposed output against the pinned output schema. A retry-candidate preflight separately requires the latest review to confirm the effect was not applied, checks attempt budget, dependencies, lease state, pinned capability contract and retained approval where required. The success path remains preflight-only. A separate atomic non-application resolution marks an interrupted step failed only when the latest review confirms no effect was applied. An atomic success resolution can persist a schema-validated output and mark an interrupted step succeeded only when the latest review confirms the external effect, the capability contract is unchanged and no live lease exists. Both append their workflow resolution event and, when needed, transition the task to failed with its own audit event in the same SQLite transaction; neither replays a handler or resumes downstream execution. A durable idempotency ledger is now enforced before coordinator handler dispatch. Existing records, changed request payloads and uncertain dispatch states block handler replay. This reduces accidental duplicate execution but does not provide exactly-once effects across SQLite and arbitrary external providers. Provider-side idempotency adapters, provider lookup/reconciliation, crash recovery that safely finalizes persisted results, OS-level isolation, resource quotas and a complete authorized retry executor remain open. Background scheduling, distributed coordination, process-level cancellation and OS-level isolation remain open. See `docs/worker-leases.md`. All draft PRs remain unmerged. The current idempotency increment is under CI verification and is not production-ready.
