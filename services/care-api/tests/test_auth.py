"""Authentication + RBAC. No database: every rejection must happen before a handler touches PHI."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from care_api import main
from care_api.auth import AUTH, AuthError, DevIssuer, Reader, User, Verifier

ISS, CLIENT = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST", "console-client"


@pytest.fixture
def signer():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def cognito_token(key, **overrides):
    now = int(time.time())
    claims = {"iss": ISS, "sub": "u-1", "username": "nurse.jane", "client_id": CLIENT, "token_use": "access",
              "cognito:groups": ["nurse"], "iat": now, "exp": now + 600} | overrides
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


def test_valid_cognito_access_token(signer):
    user = Verifier(ISS, CLIENT, lambda _t: signer.public_key()).verify(cognito_token(signer))
    assert user == User("u-1", "nurse.jane", frozenset({"nurse"})) and user.can("read") and user.can("act")


@pytest.mark.parametrize("overrides, reason", [
    ({"iss": "https://evil.example"}, "other issuer"),
    ({"exp": int(time.time()) - 120}, "expired"),
    ({"client_id": "another-app"}, "token for another application"),
    ({"token_use": "id"}, "ID token instead of access token"),
    ({"sub": None}, "missing subject"),
])
def test_rejected_tokens(signer, overrides, reason):
    with pytest.raises(AuthError):
        Verifier(ISS, CLIENT, lambda _t: signer.public_key()).verify(cognito_token(signer, **overrides))


def test_forged_signature_and_alg_none_rejected(signer):
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = Verifier(ISS, CLIENT, lambda _t: signer.public_key())
    with pytest.raises(AuthError):
        v.verify(cognito_token(attacker))
    unsigned = jwt.encode(jwt.decode(cognito_token(signer), options={"verify_signature": False}), None,
                          algorithm="none")
    with pytest.raises(AuthError):
        v.verify(unsigned)


def test_role_matrix_and_unknown_groups_grant_nothing(signer):
    v = Verifier(ISS, CLIENT, lambda _t: signer.public_key())
    matrix = {role: v.verify(cognito_token(signer, **{"cognito:groups": [role]})) for role in
              ("nurse", "physician", "admin", "presenter", "superuser")}
    assert {r: (u.can("read"), u.can("act"), u.can("demo")) for r, u in matrix.items()} == {
        "nurse": (True, True, False), "physician": (True, True, False), "admin": (True, False, True),
        "presenter": (False, False, True), "superuser": (False, False, False)}
    assert matrix["superuser"].role == "none"


def test_dev_mode_is_refused_outside_local():
    for env, mode, extra in (("aws", "dev", {}), ("local", "oidc", {}), ("local", "magic", {})):
        cfg = main.ApiSettings(env=env, auth_mode=mode, **extra)
        with pytest.raises(RuntimeError):
            main.configure_auth(cfg)
    main.configure_auth(main.ApiSettings(env="aws", auth_mode="oidc", oidc_issuer=ISS, oidc_client_id=CLIENT,
                                         oidc_domain="https://x.auth.us-east-1.amazoncognito.com"))
    assert AUTH["dev"] is None


@pytest.fixture
def dev():
    issuer = DevIssuer()
    AUTH.update(dev=issuer, verifier=issuer.verifier())
    return issuer


def test_require_dependency(dev):
    app = FastAPI()

    @app.get("/phi")
    def phi(user: Reader) -> dict:
        return {"user": user.username}

    c = TestClient(app)
    assert c.get("/phi").status_code == 401
    assert c.get("/phi", headers={"authorization": "Bearer not-a-jwt"}).status_code == 401
    ok = c.get("/phi", headers={"authorization": f"Bearer {dev.mint('dr.khan', 'physician')}"})
    assert ok.status_code == 200 and ok.json() == {"user": "dr.khan"}
    assert c.get("/phi", headers={"authorization": f"Bearer {dev.mint('pres', 'presenter')}"}).status_code == 403


def test_api_rejects_before_touching_data(dev):
    c = TestClient(main.app)  # no lifespan: no database, so any 2xx here would mean an unprotected route
    for path in ("/api/patients", "/api/patients/p-1", "/api/patients/p-1/vitals", "/api/events", "/api/stats",
                 "/api/alerts", "/api/patients/p-1/risk", "/api/patients/p-1/summary", "/api/knowledge/P-X#1",
                 "/api/models", "/api/auth/me"):
        assert c.get(path).status_code == 401, path
    presenter = {"authorization": f"Bearer {dev.mint('pres', 'presenter')}"}
    assert c.get("/api/patients", headers=presenter).status_code == 403
    admin = {"authorization": f"Bearer {dev.mint('ops', 'admin')}"}
    assert c.post("/api/alerts/a-1/ack", json={}, headers=admin).status_code == 403
    assert c.post("/api/patients/p-1/commands", json={"cmd": "CHECKIN"}, headers=presenter).status_code == 403
    me = c.get("/api/auth/me", headers=admin).json()
    assert me["role"] == "admin" and me["can"] == {"read": True, "act": False, "demo": True}


def test_dev_login_and_config(dev, monkeypatch):
    monkeypatch.setattr(main.settings, "auth_mode", "dev")
    c = TestClient(main.app)
    assert c.get("/api/auth/config").json()["mode"] == "dev"
    tok = c.post("/api/auth/dev/token", json={"username": "nurse.jane", "role": "nurse"}).json()["access_token"]
    assert dev.verifier().verify(tok).roles == {"nurse"}
    assert c.post("/api/auth/dev/token", json={"username": "x", "role": "god"}).status_code == 422
    AUTH["dev"] = None  # as in OIDC mode: the dev endpoint does not exist
    assert c.post("/api/auth/dev/token", json={"username": "nurse.jane", "role": "nurse"}).status_code == 404


def test_websocket_requires_token_in_subprotocol(dev):
    c = TestClient(main.app)
    for protocols in ([], ["rpm.v1"], ["rpm.v1", "garbage"], ["rpm.v1", dev.mint("pres", "presenter")],
                      [dev.mint("dr.khan", "physician")]):
        with pytest.raises(WebSocketDisconnect) as exc, c.websocket_connect("/ws/live", subprotocols=protocols):
            pass
        assert exc.value.code == 1008


def test_api_responses_are_not_cacheable(dev):
    r = TestClient(main.app).get("/api/patients")
    assert r.status_code == 401 and r.headers["cache-control"] == "no-store"
