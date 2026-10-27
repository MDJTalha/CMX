# CMX Web Frontend

The frontend in `web/` is a responsive presentation layer for the decision-evaluation API currently implemented by CMX. FastAPI serves `web/index.html` at `/` and its CSS/JavaScript assets at `/app.css` and `/app.js`. API routes remain registered before the static-file mount.

## Current views

- **Overview:** live API health/version check and session-scoped assessment counters.
- **New assessment:** decision statement, desired outcome, region, optional first-year budget, editable weighted/mandatory requirements, and candidate options.
- **Decision report:** eligibility, documented requirement coverage, cost/budget and region states, evidence status, unknowns, failures, warnings and methodology note.
- **Session history:** reopen reports completed in the current page session.
- **Option library:** browse and copy candidate records from in-memory completed assessments.
- **Assessment templates:** seed software-selection, service-provider and process-improvement drafts; users must tailor and validate each framework.
- **Workspace settings:** refresh API status, export/import bounded session JSON, and clear in-memory history with confirmation.
- **Report export:** download a text report for the currently selected assessment.
- **Trust & evidence:** explain the evidence labels, authorization boundary and known implementation limits.

## API contract

- `GET /health` — service health.
- `GET /api/v1` — API metadata.
- `POST /api/v1/assessments/evaluate` — decision evaluation. Requires an authorized bearer credential with `assessment:write`.

The browser asks for the bearer credential only when the user submits an assessment. It sends the credential in the request header and does not write it to local storage, session storage, cookies, or the assessment history. Browser session history itself is held in memory and is lost on reload.

## Explicit limitations

This frontend does not imply functionality that the current API does not provide. In this version there is no user login page or browser-session/CSRF flow, server-persisted assessment history, catalogue search, web research, independent source verification, LLM-powered semantic matching, evidence freshness service, outcome tracking or workflow execution UI. A candidate's evidence label is supplied data, not an assertion that CMX independently verified it.

The documented match score is weighted coverage of explicit requirement keys; it is not a success probability. Unknown requirements remain unknown and should be validated before a decision.

## Verification

The GitHub Actions workflow runs `node --check web/app.js`, Python compilation, the automated test suite, and API/static-asset compatibility tests in `tests/test_frontend.py`. Static API/frontend compatibility tests and JavaScript syntax checks run in CI. Import validation limits file size and record counts, but imported files should still be treated as untrusted input. Visual cross-browser checks, keyboard/screen-reader review, browser end-to-end tests, and production deployment checks remain separate release activities.
