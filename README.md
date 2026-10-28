# CMX — Customer & Market Experience

**Product direction:** Decision-to-Outcome Intelligence for businesses.

CMX helps a person or organization move from an unclear need to a defensible decision, an approved implementation and a measured outcome. It is vendor-neutral: the right answer may be improving an existing process, buying software, automating a workflow, using AI, building a custom solution, consulting an expert, or taking no action.

**Principle:** understand the need before recommending a solution; verify before asserting; obtain authorization before acting; measure outcomes after implementation.

## First market hypothesis

Start with a specific SME back-office workflow decision. The first target niche will be chosen through customer interviews and paid pilot evidence—not assumed in advance. HR and administrative workflows are candidates for discovery, not yet a validated market.

## What is implemented in v0.1

- Validated Pydantic contracts for a decision request, requirements, candidate solutions and the response.
- Deterministic capability matching with separate explicit-negative and unknown states.
- First-year cost calculation from supplied setup and monthly cost values.
- Conservative handling of unverified prices and regional availability.
- Explainable weighted requirement-coverage score. This is **not** a probability of success.
- FastAPI endpoints for health, metadata and decision evaluation.
- Server-derived permission enforcement and bearer service-key verification for the assessment evaluation endpoint.
- Server-side credential expiry/revocation, digest-only secret storage and a hash-chained credential issue/revoke ledger.
- Operator CLI to issue or revoke service credentials; no public credential-administration endpoint.
- Automated decision, API, credential, workflow and persistence tests, plus GitHub Actions CI.

The deterministic decision engine evaluates candidate records supplied in the request. A separate, authenticated source-discovery endpoint can retrieve bounded Brave Search result metadata when configured; those results are unverified leads, not verified claims, vendor recommendations or pricing. CMX does not crawl full pages, independently verify sources, retrieve live prices, call an LLM, train ML models, execute business actions or run advertisements.

## Reasoning kernel status — under active development

The dependent draft pull-request series includes validated decision contracts, a task lifecycle kernel, bounded JSON Schema validation, tenant-scoped authorization policy, durable SQLite task/audit records, persisted workflow plans and step results, approval gates, and a controlled coordinator that runs explicitly injected handlers in dependency order. The assessment HTTP route now requires a server-issued bearer service credential with the assessment:write permission.

This remains an engineering foundation, not a production autonomous business platform. Service-key authentication does not provide OIDC, human login, MFA, a browser session, rate limiting, managed secrets, production-scale persistence or resource-level tenant enforcement for future stored assessments. Live search metadata retrieval is available through the optional Brave Search adapter; no LLM provider or full-page crawling adapter is connected. Never construct PrincipalContext from client-supplied JSON. The coordinator's default in-process handlers still cannot be forcibly stopped. An opt-in SubprocessJSONHandler adapter now enforces parent-side deadlines and bounds JSON streams for explicitly configured fixed commands; it is process isolation, not an OS sandbox. A timeout means the side-effect outcome is unknown and must not trigger blind replay. SQLite is a single-node reference backend. All changes remain in draft pull requests and require review before merging.

## Quick start — Windows PowerShell

Requirements: Python 3.11 or later and internet access for the initial dependency installation.

From the repository folder, run:

    .\scripts\start.ps1

Open:
- Application API: http://127.0.0.1:8000
- Interactive API documentation: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

The health and metadata endpoints are public. Evaluation requests require a bearer service credential. The first setup creates an empty local credential database at data/cmx-auth.sqlite3 when the protected route is used. Install the project and issue a key from a trusted local operator terminal:

    python -m pip install -e ".[dev]"
    $env:CMX_AUTH_DB_PATH = ".\data\cmx-auth.sqlite3"
    python -m cmx.auth_cli issue --subject-id svc_assessments --tenant-id tenant_alpha --permission assessment:write --expires-in-days 30

The credential CLI prints the bearer token once. Store it securely. Do not paste the token into source code, GitHub issues or logs. The .gitignore excludes the local SQLite database files. To revoke a key, use the credential ID reported by the CLI:

    python -m cmx.auth_cli revoke --credential-id cred_REPLACE_WITH_32_HEX_CHARACTERS

To run the tests in a separate PowerShell session:

    .\scripts\test.ps1

If local PowerShell policy blocks running the script, use a process-scoped policy only for that terminal session, consistent with your organization's device policy.

## API endpoints

- GET /health
- GET /api/v1
- POST /api/v1/assessments/evaluate — requires a server-issued bearer credential with assessment:write
- POST /api/v1/research/search — requires the same permission and a configured server-side Brave Search key

The evaluation endpoint accepts a problem statement, desired outcome, target region, optional first-year budget, weighted requirements and candidate records. It returns candidate eligibility, documented requirement coverage, known failures, unknowns, budget/region status and warnings. The research endpoint returns bounded search-result titles, snippets, HTTPS URLs and retrieval timestamps; it does not verify source claims or pricing.

Example request: examples/assessment.json

## Optional live source discovery

Live source discovery uses the Brave Search API from the **server**, never directly from the browser. Set `CMX_BRAVE_SEARCH_API_KEY` in the API process or deployment's secret environment configuration. Do not commit the key, expose it in frontend settings, or place it in browser storage. If the key is absent or the provider is unavailable, the endpoint returns a service-unavailable response rather than fabricating results.

The authenticated `POST /api/v1/research/search` endpoint accepts a bounded query, region and `max_results` (1–10). It returns actual search-result metadata marked as unverified. Results are leads for human review; they are not proof of product capabilities, security certifications, regional availability or current pricing. Search queries include the user's problem and outcome text, so users should remove confidential or sensitive details before using discovery. The endpoint has no full-page crawler and does not auto-create candidate records.

## Decision and evidence rules

- Requirement matching uses stable machine keys; this version does not attempt fuzzy or LLM-based semantic matching.
- An explicitly unsupported mandatory requirement excludes a candidate.
- An unknown mandatory capability triggers validation rather than being treated as supported or unsupported.
- Only a verified first-year cost that exceeds the user's budget causes a hard budget exclusion.
- Evidence labels and URLs are supplied data, not self-verifying proof. Only a controlled, trusted catalogue process should mark records as verified.
- No candidate is recommended if the requirements are absent or the available evidence is insufficient.

## Roadmap

See docs/roadmap.md for the staged plan, docs/architecture.md for the trust boundary and scoring semantics, docs/authorization-model.md for service credentials, docs/isolated-execution.md for subprocess handler limits, and docs/worker-leases.md for worker reservation and fencing limits.

## Brand

**CMX** means **Customer & Market Experience**. It is the chosen working brand; trademark and domain clearance remain separate tasks.


### Worker execution safety

Workflow execution uses tenant-scoped SQLite worker leases with monotonically increasing fencing tokens. The coordinator claims a pending step before invoking its handler and requires the same lease token for persisted start/completion/failure transitions. Concurrent coordinator calls leave a step with a live lease untouched. Expired or uncertain work is not automatically replayed. See [worker lease documentation](docs/worker-leases.md) for current limits, including the lack of lease heartbeats and distributed scheduling.



### Interrupted-work reconciliation and retry authorization

CMX records operator reconciliation reviews for interrupted workflow steps and supports an explicit, audit-only retry-authorization record. The record is bound to the latest `effect_not_applied` review, the immutable plan hash, the pinned capability contract, the attempt budget, dependency completion, current lease state, and any required input-bound approval.

A separate transactional consumption operation can mark an authorization as consumed once. It rejects stale reviews, mismatched workflow evidence, active leases, exhausted attempts, and duplicate consumption. Both authorization and consumption are recorded in the workflow audit chain.

**Safety boundary:** authorization and consumption are not execution. Neither operation resets the interrupted step, claims a worker lease, invokes a handler, or starts a retry. A safe retry executor with external-side-effect idempotency, crash recovery, and provider-specific reconciliation is not yet implemented. Never treat an authorization or consumption event as permission to replay a handler automatically.
