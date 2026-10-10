# ADR 0012: Clinician authentication and role-based access

**Status:** accepted · 2026-09-25

## Context

Until week 4 the API trusted an `X-User` header set by the console. That is fine for a demo, but not acceptable for
PHI: anyone who could reach the API could claim any identity, and the audit log recorded whatever they claimed.

## Decision

**Identity provider: Amazon Cognito.**

- A user pool with no self sign-up (admin-created accounts) and a strong password policy.
- Optional TOTP MFA, which production sets to ON.
- The groups `nurse`, `physician`, `admin` and `presenter` are the roles.

**Console sign-in: authorization code + PKCE, public client (no secret in the browser).**

- Access tokens last 60 minutes and refresh silently; refresh tokens last 12 hours (one shift).
- Tokens are held in `sessionStorage`, never in URLs.
- The WebSocket token travels as a subprotocol entry, because browsers cannot set an Authorization header on a
  WebSocket.

**API verification.**

- RS256 signature checked against the pool's JWKS, plus issuer and expiry.
- `token_use=access` and `client_id` must match, so ID tokens and other applications' tokens are rejected.
- Roles come from `cognito:groups`; unknown groups grant nothing.

**RBAC is enforced in the API.** The console only hides controls that would be refused anyway.

| Role | Read PHI | Clinical actions (ack, escalate, resolve, device commands) | Demo scenarios |
|---|---|---|---|
| nurse, physician | ✓ | ✓ | – |
| admin | ✓ | – | ✓ |
| presenter | – | – | ✓ |

**Local development runs the same code path.** With `RPM_ENV=local`, the API mints Cognito-shaped tokens from an
in-memory RSA key for the role picked on the dev sign-in screen. Dev mode refuses to start in any other
environment, and its token endpoint does not exist in OIDC mode.

**Audit.** Every record carries the verified username, role and token subject; the client-supplied headers are
gone.

## Consequences

- One verification implementation for both environments. Tests cover forged signatures, `alg=none`, the wrong
  issuer or client, expiry, ID tokens, the role matrix, and unauthenticated REST and WebSocket access.
- No public ingress in the demo: clinicians reach the console through `kubectl port-forward`, and the callback URL
  is `http://localhost:3000/callback`. Exposing the console publicly needs an ALB, WAF, and the public origin added
  to `console_urls`.
- Machine clients (the `make stats` helper, the ops live board) use dev tokens locally. On AWS they would use a
  Cognito client-credentials app client, which is not needed for the demo.
