# CMX Roadmap

Roadmap items are proposals, not implemented capabilities. Progress is gated on tests, customer validation and security review.

## Phase 0 — Decision core (current)

- [x] Structured problem, requirement and candidate contracts
- [x] Deterministic capability constraints
- [x] Explicit unknown and excluded states
- [x] First-year cost evaluation with cautious handling of unverified prices
- [x] Explainable weighted requirement coverage
- [x] FastAPI endpoints and automated tests
- [ ] Run CI and dependency security checks in GitHub Actions

## Phase 1 — Validation and persistence

- [ ] Validate one SME workflow with customer interviews and paid pilots
- [ ] Add versioned assessment templates
- [ ] Add database persistence and immutable decision/audit identifiers
- [ ] Add claim-level evidence records and expiry/freshness policies
- [ ] Add schema migration and backup/restore tests

## Phase 2 — Core reasoning kernel

- [ ] Explicit task state machine and bounded task lifecycle
- [ ] Capability/tool registry with input/output schemas, permissions and timeouts
- [ ] Plan representation with dependencies, checkpoints and retry limits
- [ ] Independent verification contracts and testable completion criteria
- [ ] Deterministic core remains usable without an LLM

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
