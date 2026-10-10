"""Authentication and role-based access for the clinician API.

On AWS the console signs users in with Amazon Cognito (authorization code + PKCE) and calls the API with the access
token. The API checks the signature against the user pool's JWKS, the issuer, expiry, `token_use` and `client_id`,
and takes roles from Cognito groups. Locally (RPM_ENV=local only) the API mints tokens of the same shape for a role
picked on the console's dev sign-in screen, so both environments run exactly the same verification and RBAC code.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, HTTPException, Request

# Role -> what it may do. PHI reads are also written to audit_log with the acting user.
ROLES = ("physician", "nurse", "admin", "presenter")  # precedence order for the audit's primary role
PERMISSIONS: dict[str, frozenset[str]] = {
    "read": frozenset({"nurse", "physician", "admin"}),   # patients, vitals, alerts, briefs, evidence
    "act": frozenset({"nurse", "physician"}),             # ack / escalate / resolve alerts, device commands
    "demo": frozenset({"presenter", "admin"}),            # scenario, discharge and lab controls for demonstrations
    "audit": frozenset({"admin"}),                        # who read or changed which patient's data
}
LEEWAY_S = 30


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class User:
    sub: str
    username: str
    roles: frozenset[str]

    @property
    def role(self) -> str:
        return next((r for r in ROLES if r in self.roles), "none")

    def can(self, permission: str) -> bool:
        return bool(self.roles & PERMISSIONS[permission])


class Verifier:
    """Validates Cognito-shaped access tokens. `key_for(token)` returns the verification key (JWKS lookup)."""

    def __init__(self, issuer: str, client_id: str, key_for: Callable[[str], object]):
        self.issuer, self.client_id, self.key_for = issuer, client_id, key_for

    def verify(self, token: str) -> User:
        try:
            claims = jwt.decode(token, self.key_for(token), algorithms=["RS256"], issuer=self.issuer,
                                leeway=LEEWAY_S, options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": False})
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {exc}") from exc
        # Cognito access tokens carry client_id (not aud); an ID token or another app's token must not work here.
        if claims.get("token_use") != "access" or claims.get("client_id") != self.client_id:
            raise AuthError("token is not an access token for this application")
        roles = frozenset(g for g in claims.get("cognito:groups", []) if g in ROLES)
        return User(sub=claims["sub"], username=claims.get("username") or claims["sub"], roles=roles)


def cognito_verifier(issuer: str, client_id: str) -> Verifier:
    jwks = jwt.PyJWKClient(f"{issuer.rstrip('/')}/.well-known/jwks.json", cache_keys=True, lifespan=3600)
    return Verifier(issuer.rstrip("/"), client_id, lambda token: jwks.get_signing_key_from_jwt(token).key)


class DevIssuer:
    """Local development only: an in-memory RSA key that signs Cognito-shaped access tokens."""

    issuer = "rpm-dev-issuer"
    client_id = "rpm-console-dev"

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = uuid.uuid4().hex[:16]

    def mint(self, username: str, role: str, ttl_s: int = 8 * 3600) -> str:
        """role: one role, or several joined with "+" (a Cognito user can be in several groups too)."""
        roles = role.split("+")
        if any(r not in ROLES for r in roles):
            raise AuthError(f"unknown role {role}")
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": f"dev-{username}", "username": username, "client_id": self.client_id,
                  "token_use": "access", "cognito:groups": roles, "iat": now, "exp": now + ttl_s,
                  "jti": uuid.uuid4().hex}
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self.kid})

    def verifier(self) -> Verifier:
        public = self._key.public_key()
        return Verifier(self.issuer, self.client_id, lambda _token: public)


# ---------------------------------------------------------------------- FastAPI wiring
AUTH: dict = {}  # "verifier": Verifier, "dev": DevIssuer | None — set by main.lifespan


def bearer(value: str | None) -> str | None:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


def authenticate(token: str | None) -> User:
    if not token:
        raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        return AUTH["verifier"].verify(token)
    except AuthError as exc:
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}) from exc


def current_user(request: Request) -> User:
    user = authenticate(bearer(request.headers.get("authorization")))
    request.state.user = user
    return user


def require(permission: str):
    def dep(user: Annotated[User, Depends(current_user)]) -> User:
        if not user.can(permission):
            raise HTTPException(403, f"role '{user.role}' may not {permission}")
        return user
    return dep


Reader = Annotated[User, Depends(require("read"))]
Actor = Annotated[User, Depends(require("act"))]
Presenter = Annotated[User, Depends(require("demo"))]
Auditor = Annotated[User, Depends(require("audit"))]
