# Phase 3 — Evidence retrieval and model adapters

## Scope implemented in this increment

- Provider-neutral `ModelProvider` contract and `ModelGateway` with explicit provider selection, request bounds, configured timeout passed to adapters, per-call/aggregate cost budgets, call-count limits, and no silent vendor fallback.
- `ApprovedEvidenceStore` stores only explicitly approved records. It does not crawl arbitrary URLs or claim that source content is independently verified.
- Tenant-scoped lexical retrieval from approved documents with approval identity/time, source URI/type, content SHA-256, observation/retrieval timestamps, expiry, freshness status, relevance score, and bounded excerpts.
- Revocation support for approved source records.
- Injection-indicator detection and an explicit untrusted-evidence serialization boundary. Detection is heuristic and not a complete prompt-injection classifier.
- A deterministic evaluation harness for required-term coverage, forbidden-term detection and estimated-cost ceilings.
- Automated tests for tenant isolation, freshness, expiry, revocation, source URI restrictions, injection indicators, provider selection, budgets and evaluation scoring.

## Evidence ingestion contract

A trusted operator or ingestion workflow must validate a source and approve it before calling `ApprovedEvidenceStore.approve`. The library does not fetch the `source_uri`, verify vendor claims, or infer trust from a URL. Approval metadata is a record of the workflow's assertion, not independent proof that the assertion is correct. Keep ingestion credentials and the SQLite database protected.

## Provider adapter contract

Implementations of `ModelProvider` must:
1. Have a unique stable `provider_name`.
2. Enforce the supplied transport timeout at the network/client layer.
3. Return provider/model identity, token counts and a defensible cost estimate.
4. Never place secrets in prompts, logs or exception messages.
5. Treat retrieved excerpts as untrusted data.
6. Avoid automatic provider switching unless a separately reviewed policy explicitly allows it.

`ModelGateway` checks elapsed time after a synchronous call returns, but Python cannot safely interrupt an arbitrary blocked adapter. Therefore the provider implementation must enforce the timeout itself. Cost is only as reliable as provider usage metadata and configured pricing; estimates are not invoices.

## Known limitations / Phase 3 exit criteria still open

- No hosted vendor SDK, local model runtime, credentials, or real provider connectivity is configured by this increment.
- Lexical retrieval is deterministic baseline retrieval, not embedding/vector retrieval or semantic reranking.
- No crawler, source authenticity verifier, vendor-data ingestion connector, or automated refresh workflow is included.
- Injection indicators are heuristic. They do not prove malicious intent or guarantee resistance to novel attacks.
- The evaluation harness is a baseline; a representative, human-reviewed evaluation corpus and factual-support/constraint-adherence review are still needed.
- Operational telemetry, per-tenant quotas, persistent budget accounting across process restarts, secret management, and provider-specific integration tests remain to be completed before production.
