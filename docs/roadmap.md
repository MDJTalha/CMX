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
- [ ] Complete human identity integration and browser sessions/MFA where needed (an optional RS256 OIDC bearer verifier, server-controlled tenant membership/permissions, optional MFA-claim enforcement, API auth-mode selection and local grant/revoke commands are implemented; browser sessions, CSRF protection, login/logout, MFA enrollment and full provider conformance review remain open)
- [x] Validate capability schemas and plan inputs against bounded JSON Schema Draft 2020-12 contracts
- [x] Add a controlled coordinator for explicitly injected capability handlers and topologically ordered plans
- [x] Persist immutable workflow plans, step states/results, approvals and hash-chained workflow audit events
- [x] Bind approval records to plan hash, inputs and capability-contract fingerprint
- [x] Add an opt-in fixed-command subprocess adapter with parent-enforced deadlines, bounded JSON streams and explicit unknown-outcome errors
- [ ] Add OS-level sandbox/descendant-process supervision and resource quotas; provider-specific adapters and safe retry execution remain unimplemented
- [x] Add a tenant-scoped durable idempotency ledger with canonical request binding, hash-chained transition events, duplicate-key conflict detection, and fail-closed reservation/dispatch/success/uncertain states; coordinator refuses to invoke a handler when a prior record exists
- [x] Add a provider-neutral reconciliation observation contract with strict operation/key binding, freshness checks, and fail-closed decisions; it does not implement a real provider adapter or authorize replay
- [x] Add a durable, short-lived, tenant-bound, single-use recovery authorization ticket with distinct approver/executor identities and hash-chained issue/consume audit events; no retry executor is implied
- [ ] Complete background worker scheduling and full recovery/reconciliation for uncertain outcomes (a tenant-scoped SQLite queue with idempotent enqueue, expiring claims, fencing tokens, bounded retry scheduling and coordinator delegation is implemented as a single-node reference; scheduler heartbeat renewal is implemented and tested in CI; production broker/HA, service-managed shutdown, safe downstream resumption and a fully integrated provider-aware retry/idempotency executor remain open; evidence-backed reviews, read-only inventory, resolution preflight and atomic terminal resolutions are implemented)
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

The coordinator has bounded execution and explicit worker lease fencing. Recovery reviews, read-only uncertain-step inventory, schema-validated resolution preflight, and atomic terminal resolutions are implemented; these do not replay handlers or resume downstream execution. The durable idempotency ledger blocks duplicate dispatch after an existing record. A provider-neutral reconciliation contract now rejects mismatched or stale evidence and never authorizes replay. A separate SQLite-backed ticket now supports short-lived, tenant-scoped, single-use recovery authorization with separate approver/executor identities and hash-chained audit events. This ticket is not connected to a retry executor; consumption intentionally burns the ticket even if a later process crashes. No claim of exactly-once external effects is made. Phase 2 remains open for real provider-specific adapters, an integrated crash-safe retry/recovery executor, background worker scheduling, OS-level sandboxing and resource quotas, OIDC/session/MFA, and end-to-end adversarial testing. The SQLite implementation is a single-node reference, not a distributed coordination guarantee. Draft PRs remain unmerged and must pass CI and review before integration.


## Latest Phase 2 increments

- POSIX subprocess handler timeouts and output-limit failures now terminate the supervised process group with bounded SIGTERM/SIGKILL escalation; regression coverage checks that a descendant cannot perform a delayed write after timeout. Windows descendant cleanup still requires a Job Object or dedicated worker/container supervisor.
- A durable single-node scheduler now persists tenant-scoped jobs, claims them transactionally with fencing tokens, delegates execution to the coordinator, and uses bounded retries for scheduler exceptions. This does not provide distributed queue guarantees or exactly-once external effects.

- Scheduler workers in wall-clock mode now renew their queue lease while the coordinator runs and refuse result commits after heartbeat loss; deterministic explicit-time tests do not start a real-time heartbeat.

- Optional OIDC API authentication now verifies RS256 issuer/audience/JWKS/time claims and derives tenant permissions only from the local membership store; service-key mode remains the default. This is not a browser-session implementation or production OIDC certification.
