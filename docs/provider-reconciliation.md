# Provider reconciliation contract

## Scope

The src/cmx/recovery.py module defines a provider-neutral contract for reporting and evaluating evidence about interrupted external operations. It is deliberately not a provider integration and does not execute a retry.

## Adapter requirements

A provider adapter implements ProviderReconciliationAdapter.lookup(operation_id, idempotency_key) and returns a validated ProviderObservation. Before returning it, the adapter must authenticate the provider response, bind it to the requested operation and key, and retain an evidence reference. Timeouts, eventual-consistency gaps, absent records, malformed responses, and provider errors must map to unknown, not not_applied.

The repository contains no configured provider-specific adapter. Do not treat the protocol as proof that any real provider has been integrated.

## Decision policy

- evidence_rejected: provider, operation ID, or idempotency key mismatch.
- manual_review: unknown result, stale/future evidence, unsupported operation state, or no declared provider idempotency contract.
- provider_applied: provider reports application; a trusted result still needs schema validation and a separate audited workflow-resolution transaction.
- retry_candidate: fresh bound non-application evidence with the provider idempotency contract enabled.

Every decision has safe_to_replay=false and requires separate single-use authorization. A retry candidate is not an authorization, and this module never invokes a handler, changes ledger state, or marks a workflow successful.

## Limits and production gates

The freshness window is a policy bound, not cryptographic proof. The adapter must enforce provider authentication and integrity. Provider-specific result schemas, signatures/webhook verification, rate limits, reconciliation semantics, durable retry authorization consumption, and an end-to-end retry executor remain separate work. Never enable automatic replay until those pieces are implemented and adversarially tested for the selected provider.
