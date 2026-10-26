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
- [ ] Connect a real authentication/session verifier and persist identity-bound audit records
- [x] Validate capability schemas and plan inputs against bounded JSON Schema Draft 2020-12 contracts
- [ ] Add a constrained executor interface
- [x] Add file-backed SQLite task persistence, optimistic version checks and transactional audit append tests

## Phase 3 — Evidence retrieval and model adapters

- [ ] Pluggable LLM provider interface with no provider lock-in
- [ ] Retrieval over approved documents and curated vendor records
- [ ] Source provenance, retrieval timestamps and freshness checks
- [ ] Prompt-injection and untrusted-content tests
- [ ] Evaluation set measuring factual support, constraint adherence and cost

## Phase 4 — Secure organizational product

- [ ] Authentication, authorization and tenant isolation
- [ ] Secrets management, rate limits, quotas and abuse protections
- [ ] Privacy/data retention controls and deletion workflows
- [ ] Human approval gates for consequential operations
- [ ] Observability, incident response and production security review

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

The current development branch contains task-state and capability-registry contracts, bounded JSON Schema validation, fail-closed tenant-scoped authorization policy, verification contracts, and a file-backed SQLite task/audit reference store. The store atomically persists transitions and hash-chained audit events, scopes reads by tenant, and tests version conflicts and audit integrity. It does not verify real credentials or execute tools, and SQLite is not yet a production multi-node backend. See docs/reasoning-kernel.md, docs/authorization-model.md and docs/task-persistence.md.
