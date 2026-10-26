# CMX Authorization Model

## Purpose

This increment establishes a fail-closed authorization policy contract for capability access. A real authentication provider/session verifier is not yet connected.

## Identity trust boundary

`PrincipalContext` is an internal model to be populated only after a trusted adapter verifies the caller's actual credential. It contains:
- Stable subject and tenant identifiers.
- Trusted issuer and credential identifier metadata.
- Permission grants from the trusted identity/policy layer.
- Issue and expiry timestamps.
- An authentication verification flag that defaults to false.

Never accept a serialized `PrincipalContext`, `authentication_verified`, tenant ID, or permission list directly from an untrusted client as proof of identity. Those values must be derived by server-side authentication and authorization middleware. The model validates context shape; it does not verify a token, signature, password, session or issuer.

## Authorization rules

1. A missing or wrong-type context is rejected.
2. The context must indicate successful upstream credential verification.
3. The context must not be future-dated or expired.
4. The resource tenant must match the principal tenant.
5. Every permission declared by the capability must be present.
6. Invalid permission policy strings fail closed.
7. Unknown or disabled capabilities remain unavailable.
8. Side-effecting capabilities continue to require explicit approval, and plan inputs are schema-validated before any future executor is called.

Authorization denials expose stable, non-sensitive reason codes such as `unauthenticated`, `identity_expired`, `tenant_mismatch` and `missing_permission`. They do not reveal the complete missing-permission list or protected resource details.

## Testable boundaries

The automated tests cover verified access, absent/unverified context, future issue time, expiration, tenant mismatch, insufficient permission, invalid permission keys, immutable principal data and registry integration.

The injectable epoch clock is available for deterministic tests; production code should normally use the system clock or an explicitly trusted clock source.

## Not implemented yet

- No OAuth/OIDC token or cookie validation.
- No password or MFA flow, session store, logout/revocation, or identity-provider integration.
- No persistent organization membership or policy database.
- No durable authorization decision audit log.
- No public endpoint accepting authenticated principals.

The policy layer must not be described as production authentication. Before public use, connect a real identity adapter, derive tenant membership and permission grants from trusted server-side records, implement revocation and audit logging, and test the complete HTTP request boundary including cross-tenant and IDOR/BOLA cases.
