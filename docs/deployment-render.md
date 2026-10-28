# CMX API deployment blueprint (Render)

This blueprint deploys the Python API separately from the static Vercel frontend. It does not deploy anything by itself: create a Render Blueprint from this repository and review the plan, environment variables, and persistent-disk settings before applying it.

## Required configuration

1. Set `CMX_CORS_ORIGINS` to the exact HTTPS origin of the deployed frontend, without a path or trailing slash. For local testing, use the local origin(s) explicitly; do not use `*`.
2. Set `CMX_BRAVE_SEARCH_API_KEY` as a Render secret environment variable. Never commit the key or enter it in the browser.
3. The blueprint mounts a persistent disk for the SQLite authentication and assessment databases. Do not remove the disk while relying on those records.
4. After deployment, copy the HTTPS API base URL into the frontend's API settings and use its health check. Then configure and issue a service credential using the repository's operator CLI from a trusted environment; do not expose operator credentials in the browser or public logs.

## Release verification checklist

- [ ] Render build and startup succeed.
- [ ] `GET /health` responds over HTTPS.
- [ ] Frontend API settings point to the deployed API URL.
- [ ] `CMX_CORS_ORIGINS` exactly matches the frontend origin; browser preflight succeeds.
- [ ] An unauthenticated protected request is rejected.
- [ ] An authorized request succeeds only with the expected permission.
- [ ] Live search returns actual provider results when the secret is configured.
- [ ] Search returns a service-unavailable error when provider configuration is absent; it must not fabricate results.
- [ ] Browser UI labels search results as unverified source leads.
- [ ] Restart/redeploy does not lose the mounted SQLite files.

## Important limitations

This is a deployment starting point, not evidence of a live deployment or production certification. The API currently uses SQLite and service credentials by default; assess operational, backup, rate-limit, abuse-monitoring, and tenant-isolation requirements before handling real customer data. Search metadata does not independently verify vendor claims or current pricing. Keep sensitive business details out of search queries.
