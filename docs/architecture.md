# CMX Decision Engine — Architecture

## Scope of this increment

This repository's initial product increment implements a deterministic, request-scoped decision evaluator and a FastAPI wrapper. The dependent draft pull requests add reasoning-kernel and bounded JSON Schema contracts, but they are not yet a production workflow runtime. There is no persistence, authentication, external model integration, retrieval, live catalogue feed, workflow execution or advertising.

## Request lifecycle

1. Pydantic validates the incoming assessment contract.
2. The evaluator checks explicit capability claims against requirements.
3. Explicitly unsupported mandatory requirements cause exclusion.
4. Unknown capability or region claims cause a validation-needed state rather than an unsupported inference.
5. A known, verified first-year cost above the stated budget causes exclusion. Unverified pricing cannot create false certainty.
6. Candidates are ordered by eligibility first, then documented weighted requirement coverage.
7. The response separates an eligible candidate from a provisional lead.

## Scoring semantics

The score is the weight of explicitly documented matching capabilities divided by total requirement weight. Unknowns contribute zero. This score is not a probability of success, confidence estimate, vendor quality score or proof of feature support.

Requirements use stable machine keys. The core engine intentionally does not use substring matching or LLM-based semantic matching, because that would make eligibility decisions less predictable.

## Evidence trust boundary

Evidence status and source URLs are inputs. This service does not fetch, open or independently validate sources. The value "verified" must only be assigned by a trusted catalogue curation process; it must not be accepted at face value from an untrusted public request in a production deployment.

Before production, evidence records need source title, owner, retrieval time, verification method, claim-level provenance and freshness policy. Unsupported claims and missing evidence must remain visible.

## Safety and authorization boundary

The evaluator makes recommendations only. It has no code for purchases, payments, messages, account changes or external execution. Any future action capability must have scoped permissions, audit records, idempotency where applicable, bounded retries, failure handling and human approval for consequential actions.

## Known limitations

- No authentication or tenant isolation.
- No persistent assessment history.
- No live pricing or product data.
- No LLM, ML, neural network or quantum-computing integration.
- No ROI prediction; only input-based comparisons are planned.
- No independent source verification.
- USD-only cost fields in this first contract.
- Not intended for public internet exposure until security controls are implemented and reviewed.
