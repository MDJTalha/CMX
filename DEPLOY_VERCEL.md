# Deploying the CMX frontend to Vercel

This configuration deploys the dependency-free browser frontend as a static site. It does **not** deploy the FastAPI backend.

## Deploy

1. Sign in to Vercel and choose **Add New → Project**.
2. Import `MDJTalha/CMX` from GitHub.
3. Select branch `deploy/vercel-frontend` for this initial deployment.
4. Keep the project root at the repository root. Vercel reads `vercel.json` and publishes the `frontend/` directory.
5. Deploy and wait for Vercel to provide the HTTPS deployment URL.

## Backend is required for live assessments

Before using real assessments, deploy the FastAPI API separately to a Python-capable host. Configure its environment securely, including a persistent `CMX_AUTH_DB_PATH`, short-lived least-privilege credentials, and `CMX_CORS_ORIGINS` set to the exact Vercel production origin (for example, `https://your-project.vercel.app`). Do not use `*` for production CORS. Ensure the database path is on persistent storage and that backups and restore have been tested.

In the Vercel frontend, enter the API's HTTPS base URL in the API connection field, then run the public health check. Issue an assessment-scoped service credential on the backend through a trusted operator environment; never place it in source control or Vercel build-time variables. The frontend holds the entered credential in page memory only, but this is a developer/testing workflow, not production user login.

## Launch limitations

- A successful Vercel frontend deployment alone does not mean CMX's assessment API is online.
- The current frontend does not provide production OIDC login, MFA, or managed browser sessions.
- Use only non-sensitive test data until backend security, tenant isolation, persistence, monitoring, and recovery have been independently verified.
