"""The authlib OIDC flow, tested against a fake Okta.

These run the real /auth/okta routes end to end, then check that authlib
rejects the same bad tokens the hand-written version rejected.
"""

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx  # authlib 1.9 is built on httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app.auth import oidc
from app.auth.bridge import AuthBridgeError, Principal
from app.auth.tokens import verify_access_token

ISSUER = "https://fake-okta.example/oauth2/default"
CLIENT_ID = "test-client"
CLIENT_SECRET = "test-secret"
REDIRECT_URI = "http://testserver/auth/okta/callback"


def make_key(kid: str):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid=kid, alg="RS256", use="sig")
    return private, jwk


PRIVATE_KEY, PUBLIC_JWK = make_key("key-1")
OTHER_KEY, _ = make_key("key-1")  # same kid, different key: a forged signature


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign(claims: dict, *, key=PRIVATE_KEY, kid="key-1") -> str:
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def base_claims(nonce: str) -> dict:
    now = int(time.time())
    return {
        "iss": ISSUER, "aud": CLIENT_ID, "sub": "00u123", "iat": now, "exp": now + 300,
        "nonce": nonce, "email": "hr.manager@acme.example", "groups": ["gk-hr-manager"],
    }


def hs256_with_public_key(claims: dict) -> str:
    # Classic attack: HMAC-sign a token using the IdP's *public* key as the secret.
    public_pem = jwt.PyJWK(PUBLIC_JWK).key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    header = b64url(json.dumps({"alg": "HS256", "kid": "key-1"}).encode())
    payload = b64url(json.dumps(claims).encode())
    signature = b64url(hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def unsigned(claims: dict) -> str:
    header = b64url(json.dumps({"alg": "none", "kid": "key-1"}).encode())
    return f"{header}.{b64url(json.dumps(claims).encode())}."


# How the fake Okta builds the ID token. Each takes the nonce from the login request.
GOOD = lambda n: sign(base_claims(n))  # noqa: E731


@pytest.fixture
def okta(monkeypatch):
    monkeypatch.setattr(oidc.settings, "okta_issuer", ISSUER)
    monkeypatch.setattr(oidc.settings, "okta_client_id", CLIENT_ID)
    monkeypatch.setattr(oidc.settings, "okta_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(oidc.settings, "okta_redirect_uri", REDIRECT_URI)
    monkeypatch.setattr(oidc, "_okta", None)

    fake = {"challenge": None, "nonce": None, "make_id_token": GOOD, "issuer": ISSUER, "id_token": True}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": fake["issuer"],
                "authorization_endpoint": f"{ISSUER}/v1/authorize",
                "token_endpoint": f"{ISSUER}/v1/token",
                "jwks_uri": f"{ISSUER}/v1/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
            })
        if path.endswith("/v1/keys"):
            return httpx.Response(200, json={"keys": [PUBLIC_JWK]})
        if path.endswith("/v1/token"):
            form = parse_qs(request.content.decode())
            basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            derived = b64url(hashlib.sha256(form["code_verifier"][0].encode()).digest())
            if (request.headers.get("authorization") != f"Basic {basic}"
                    or form["code"] != ["good-code"] or derived != fake["challenge"]):
                return httpx.Response(400, json={"error": "invalid_grant"})
            body = {"access_token": "at", "token_type": "Bearer", "expires_in": 3600}
            if fake["id_token"]:
                body["id_token"] = fake["make_id_token"](fake["nonce"])
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    monkeypatch.setattr(oidc, "CLIENT_KWARGS", {"transport": httpx.MockTransport(handler)})

    # The bridge has its own DB tests (test_bridge.py); here just record what it was given.
    async def fake_principal_for(claims):
        fake["bridged_claims"] = claims
        if fake.get("bridge_error"):
            raise AuthBridgeError(fake["bridge_error"])
        return Principal("user-1", "tenant-1", claims["email"], "hr_manager", ("docs:read",))

    monkeypatch.setattr(oidc, "principal_for", fake_principal_for)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-session-secret", session_cookie="gk_login")
    app.include_router(oidc.router)
    client = TestClient(app)
    client.fake = fake
    return client


def start_login(client) -> str:
    resp = client.get("/auth/okta/login", follow_redirects=False)
    assert resp.status_code in (302, 307)
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    client.fake["challenge"] = query["code_challenge"][0]
    client.fake["nonce"] = query["nonce"][0]
    return query["state"][0]


def finish_login(client, state: str, code: str = "good-code"):
    return client.get("/auth/okta/callback", params={"code": code, "state": state})


def test_full_login_returns_access_token(okta):
    resp = finish_login(okta, start_login(okta))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["principal"] == {
        "user_id": "user-1", "tenant_id": "tenant-1", "email": "hr.manager@acme.example",
        "role": "hr_manager", "scopes": ["docs:read"],
    }
    assert body["token_type"] == "Bearer" and body["expires_in"] == 900
    claims = verify_access_token(body["access_token"])
    assert (claims["sub"], claims["tenant_id"], claims["role"], claims["scope"]) == ("user-1", "tenant-1", "hr_manager", "docs:read")
    # The bridge only ever sees claims that passed validation.
    assert okta.fake["bridged_claims"]["iss"] == ISSUER
    assert okta.fake["bridged_claims"]["groups"] == ["gk-hr-manager"]


def test_bridge_refusal_is_403(okta):
    okta.fake["bridge_error"] = "user is in no Gatekeeper group"
    resp = finish_login(okta, start_login(okta))
    assert resp.status_code == 403
    assert "no Gatekeeper group" in resp.text


def test_rejected_token_never_reaches_bridge(okta):
    okta.fake["make_id_token"] = BAD_ID_TOKENS["forged signature"]
    finish_login(okta, start_login(okta))
    assert "bridged_claims" not in okta.fake


BAD_ID_TOKENS = {
    "forged signature": lambda n: sign(base_claims(n), key=OTHER_KEY),
    "wrong issuer": lambda n: sign({**base_claims(n), "iss": "https://evil.example"}),
    "wrong audience": lambda n: sign({**base_claims(n), "aud": "someone-else"}),
    "expired": lambda n: sign({**base_claims(n), "exp": int(time.time()) - 3600}),
    "nonce mismatch (replay)": lambda n: sign(base_claims("some-other-login")),
    "missing nonce": lambda n: sign({k: v for k, v in base_claims(n).items() if k != "nonce"}),
    "alg none": lambda n: unsigned(base_claims(n)),
    "HS256 key confusion": lambda n: hs256_with_public_key(base_claims(n)),
}


@pytest.mark.parametrize("case", BAD_ID_TOKENS)
def test_bad_id_tokens_rejected(okta, case):
    okta.fake["make_id_token"] = BAD_ID_TOKENS[case]
    resp = finish_login(okta, start_login(okta))
    assert resp.status_code in (400, 401), f"{case}: {resp.status_code} {resp.text}"


def test_discovery_cannot_change_the_issuer(okta):
    # A tampered discovery doc AND token both claim an attacker's issuer.
    okta.fake["issuer"] = "https://evil.example"
    okta.fake["make_id_token"] = lambda n: sign({**base_claims(n), "iss": "https://evil.example"})
    assert finish_login(okta, start_login(okta)).status_code == 401


def test_missing_id_token_rejected(okta):
    okta.fake["id_token"] = False
    assert finish_login(okta, start_login(okta)).status_code == 502


def test_state_can_only_be_used_once(okta):
    state = start_login(okta)
    assert finish_login(okta, state).status_code == 200
    assert finish_login(okta, state).status_code == 400


def test_callback_from_another_browser_rejected(okta):
    state = start_login(okta)
    okta.cookies.clear()  # the victim's browser never started this login
    assert finish_login(okta, state).status_code == 400


def test_bad_code_rejected(okta):
    assert finish_login(okta, start_login(okta), code="stolen-code").status_code == 400


def test_okta_error_is_reported(okta):
    start_login(okta)
    resp = okta.get("/auth/okta/callback", params={"error": "access_denied", "error_description": "User is not assigned"})
    assert resp.status_code == 400
    assert "access_denied" in resp.text
