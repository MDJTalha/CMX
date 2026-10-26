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
- Automated engine/API tests and a GitHub Actions test workflow.
- Windows PowerShell scripts for local startup and testing.

The engine only evaluates supplied candidate records. It does **not** browse the web, independently verify sources, retrieve live prices, call an LLM, train ML models, execute business actions, persist assessments or run advertisements.

## Reasoning kernel status — under active development

The dependent draft pull-request series adds contracts for task lifecycle state, bounded retries, execution-plan dependency validation, a capability allowlist, human-approval requirements, completion verification, JSON Schema validation, and tenant-scoped authorization policy. It also adds a file-backed SQLite reference store for task transitions and hash-chained audit events.

These are engineering foundations, not a functioning autonomous runtime. No real authentication/session verifier, credential revocation service, live vendor retrieval, LLM provider or external executor is connected. The authorization policy consumes an internally supplied principal context and must never accept one from client-supplied JSON. SQLite is a single-node reference backend; production migration, backup/restore, retention and operational controls remain open. All changes remain in draft pull requests and require review before merging.

## Quick start — Windows PowerShell

Requirements: Python 3.11 or later and internet access for the initial dependency installation.

From the repository folder, run:

    .\scripts\start.ps1

Open:
- Application API: http://127.0.0.1:8000
- Interactive API documentation: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

To run the tests in a separate PowerShell session:

    .\scripts\test.ps1

If local PowerShell policy blocks running the script, use a process-scoped policy only for that terminal session, consistent with your organization's device policy.

## API endpoints

- GET /health
- GET /api/v1
- POST /api/v1/assessments/evaluate

The evaluation endpoint accepts a problem statement, desired outcome, target region, optional first-year budget, weighted requirements and candidate records. It returns candidate eligibility, documented requirement coverage, known failures, unknowns, budget/region status and warnings.

Example request: examples/assessment.json

## Decision and evidence rules

- Requirement matching uses stable machine keys; this version does not attempt fuzzy or LLM-based semantic matching.
- An explicitly unsupported mandatory requirement excludes a candidate.
- An unknown mandatory capability triggers validation rather than being treated as supported or unsupported.
- Only a verified first-year cost that exceeds the user's budget causes a hard budget exclusion.
- Evidence labels and URLs are supplied data, not self-verifying proof. Only a controlled, trusted catalogue process should mark records as verified.
- No candidate is recommended if the requirements are absent or the available evidence is insufficient.

## Roadmap

See docs/roadmap.md for the staged plan and docs/architecture.md for the trust boundary, scoring semantics and known limitations.

## Brand

**CMX** means **Customer & Market Experience**. It is the chosen working brand; trademark and domain clearance remain separate tasks.
