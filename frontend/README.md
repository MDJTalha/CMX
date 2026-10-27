# CMX frontend starter

This is the first UI implementation increment: a responsive, dependency-free browser workspace for drafting an assessment, checking API health, submitting a decision request and inspecting the unmodified JSON response.

## Run locally

From the repository root, start the API using the existing Windows instructions in the root README, then serve this directory from a local static server. For example, with Python installed:

```powershell
python -m http.server 4173 --directory frontend
```

Open `http://127.0.0.1:4173`. The API base defaults to `http://127.0.0.1:8000`.

## Current limitations

- The current API evaluation route requires a bearer credential. This starter deliberately does not collect, persist, or invent credentials; a protected request will be rejected until an approved authentication flow is implemented.
- Cross-origin requests require the API's CORS configuration to allow the frontend origin. CORS has not been configured by this increment.
- Request field names and nested requirement/candidate schemas must match the API contract. Check `/docs` for the exact schema; this first UI does not guess vendor records.
- Results are shown as raw JSON; visual comparison, history, session login, MFA and workflow screens are not implemented yet.
- No production deployment, browser matrix or automated frontend test runner is claimed by this increment.

## Accessibility and privacy

Includes semantic landmarks, visible focus states, skip link, keyboard-operable controls, live status announcements, reduced-motion handling and responsive layout. Do not enter confidential or personal data into this early UI. A full WCAG audit remains outstanding.
