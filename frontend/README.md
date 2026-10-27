# CMX frontend workspace

A dependency-free browser workspace for drafting an assessment, checking API health, submitting an authenticated decision request, and reviewing candidate-level evidence returned by the API.

## Run locally

From the repository root, start the API using the root README instructions, then serve the frontend:

```powershell
python -m http.server 4173 --directory frontend
```

Open `http://127.0.0.1:4173`. The API base defaults to `http://127.0.0.1:8000`.

## API authentication

The API requires a server-issued bearer credential with `assessment:write` permission to evaluate. Enter that credential in the API access section. The page holds the value only in JavaScript memory and sends it in the `Authorization: Bearer` header; it is not written to localStorage or sessionStorage. Reloading or clearing the form clears the credential. The optional tenant ID is sent as `X-CMX-Tenant` for OIDC mode only.

This is a developer/testing credential input, **not** a production login/session system. Never enter an identity-provider password, a long-lived administrator secret, or a credential on a page served over an untrusted connection. Use a short-lived, least-privileged credential and a trusted local environment. A production browser login flow, token lifecycle, logout/session expiry and MFA UX remain future work.

## CORS

The API uses the `CMX_CORS_ORIGINS` comma-separated environment variable as an explicit origin allowlist. If unset, local development allows only `http://127.0.0.1:4173` and `http://localhost:4173`. Example PowerShell override:

```powershell
$env:CMX_CORS_ORIGINS = "http://127.0.0.1:4173,http://localhost:4173"
```

Use exact trusted origins; do not use wildcard origins. Restart the API after changing this environment variable. The middleware allows the methods and headers needed for the current API and does not enable cookie credentials. CORS is a browser boundary, not authentication or authorization.

## Request and result contract

The request fields are `problem_statement`, `desired_outcome`, `region`, `max_first_year_cost_usd`, `requirements`, and `candidates`. The nested objects must match the API schema shown at `/docs`; this UI does not fabricate candidate records. The results panel displays status, summary, candidate eligibility, documented match score, unknowns, failed requirements, reasons, warnings, evidence/pricing labels and supplied source URLs. Source URLs are displayed as supplied and are **not independently verified**. The original JSON is available under Technical response.

## Verification and limitations

CI includes Playwright browser interaction tests with a mocked API, API service-token and simulated OIDC authentication tests, and CORS allowlist/preflight tests. These cover request headers and response/error rendering across the frontend and API contracts, but do not use a live identity provider or a deployed browser/API environment. Real OIDC provider integration, keyboard-only manual review, screen-reader checks, full WCAG audit, production deployment and browser/device verification remain outstanding. No production-readiness claim is made.
