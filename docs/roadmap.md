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
- [ ] Enforce handler deadlines with a tested isolation boundary and define safe retry/idempotency policy
- [ ] Add background worker scheduling and multi-worker claim/lease semantics
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

The latest authentication increment adds a persistent service credential registry, single-display random bearer key issuance, digest-only secret storage, expiry checks, one-way revocation, a hash-chained issue/revoke event ledger, an operator CLI and HTTP permission enforcement on the assessment evaluator. This is a service-to-service trust boundary, not a human identity system. See docs/authorization-model.md for usage and limitations. All draft PRs remain unmerged.
