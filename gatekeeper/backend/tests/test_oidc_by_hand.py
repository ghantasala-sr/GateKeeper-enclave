"""Reference implementation: the hand-written OIDC flow (learning/oidc_by_hand.py), tested against a fake Okta."""

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from fastapi.testclient import TestClient

from learning import oidc_by_hand as oidc

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
JWKS = {"keys": [PUBLIC_JWK]}


def id_token(*, key=PRIVATE_KEY, kid="key-1", alg="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": CLIENT_ID, "sub": "00u123", "iat": now, "exp": now + 300,
        "nonce": "n-123", "email": "hr.manager@acme.example", "groups": ["gk-hr-manager"],
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def validate(token: str, nonce: str = "n-123") -> dict:
    return oidc.validate_id_token(token, jwks=JWKS, issuer=ISSUER, client_id=CLIENT_ID, nonce=nonce)


# ---------- validate_id_token ----------

def test_valid_token_passes():
    claims = validate(id_token())
    assert claims["email"] == "hr.manager@acme.example"
    assert claims["groups"] == ["gk-hr-manager"]


@pytest.mark.parametrize(
    "token, nonce, reason",
    [
        (id_token(key=OTHER_KEY), "n-123", "forged signature"),
        (id_token(iss="https://evil.example"), "n-123", "wrong issuer"),
        (id_token(aud="someone-else"), "n-123", "wrong audience"),
        (id_token(exp=int(time.time()) - 3600), "n-123", "expired"),
        (id_token(), "different-nonce", "nonce mismatch (replay)"),
        (id_token(nonce=None), "n-123", "missing nonce"),
        (id_token(kid="unknown"), "n-123", "unknown kid"),
        (id_token(aud=[CLIENT_ID, "other"]), "n-123", "multiple audiences without azp"),
    ],
)
def test_bad_tokens_rejected(token, nonce, reason):
    with pytest.raises(oidc.TokenValidationError):
        validate(token, nonce)


def test_alg_none_rejected():
    unsigned = jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "nonce": "n-123"}, None, algorithm="none",
                          headers={"kid": "key-1"})
    with pytest.raises(oidc.TokenValidationError, match="unexpected alg"):
        validate(unsigned)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_hs256_key_confusion_rejected():
    # Classic attack: HMAC-sign a token using the IdP's *public* key as the secret.
    public_pem = jwt.PyJWK(PUBLIC_JWK).key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    now = int(time.time())
    header = b64url(json.dumps({"alg": "HS256", "kid": "key-1"}).encode())
    payload = b64url(json.dumps({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "iat": now,
                                 "exp": now + 300, "nonce": "n-123"}).encode())
    signature = b64url(hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    with pytest.raises(oidc.TokenValidationError, match="unexpected alg"):
        validate(f"{header}.{payload}.{signature}")


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = oidc.make_pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == b64url(hashlib.sha256(verifier.encode()).digest())


# ---------- full login flow against a fake Okta ----------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(oidc.settings, "okta_issuer", ISSUER)
    monkeypatch.setattr(oidc.settings, "okta_client_id", CLIENT_ID)
    monkeypatch.setattr(oidc.settings, "okta_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(oidc.settings, "okta_redirect_uri", REDIRECT_URI)
    monkeypatch.setattr(oidc, "_discovery", None)
    monkeypatch.setattr(oidc, "_jwks", None)
    monkeypatch.setattr(oidc, "_pending", {})

    fake = {"challenge": None, "nonce": None}

    def okta(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/v1/authorize",
                "token_endpoint": f"{ISSUER}/v1/token",
                "jwks_uri": f"{ISSUER}/v1/keys",
            })
        if path.endswith("/v1/keys"):
            return httpx.Response(200, json=JWKS)
        if path.endswith("/v1/token"):
            form = parse_qs(request.content.decode())
            basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            verifier = form["code_verifier"][0]
            derived = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if (request.headers.get("authorization") != f"Basic {basic}"
                    or form["code"] != ["good-code"] or derived != fake["challenge"]):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": id_token(nonce=fake["nonce"]), "token_type": "Bearer"})
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(oidc.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(okta), **kw))

    app = FastAPI()
    app.include_router(oidc.router)
    tc = TestClient(app)
    tc.fake = fake
    return tc


def start_login(client) -> str:
    resp = client.get("/auth/okta/login", follow_redirects=False)
    assert resp.status_code == 307
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    client.fake["challenge"] = query["code_challenge"][0]
    client.fake["nonce"] = query["nonce"][0]
    return query["state"][0]


def test_full_login_returns_verified_claims(client):
    state = start_login(client)
    resp = client.get("/auth/okta/callback", params={"code": "good-code", "state": state})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sub": "00u123", "email": "hr.manager@acme.example", "groups": ["gk-hr-manager"]}


def test_state_can_only_be_used_once(client):
    state = start_login(client)
    assert client.get("/auth/okta/callback", params={"code": "good-code", "state": state}).status_code == 200
    assert client.get("/auth/okta/callback", params={"code": "good-code", "state": state}).status_code == 400


def test_callback_from_another_browser_rejected(client):
    state = start_login(client)
    client.cookies.clear()  # the victim's browser never started this login
    assert client.get("/auth/okta/callback", params={"code": "good-code", "state": state}).status_code == 400


def test_bad_code_rejected(client):
    state = start_login(client)
    assert client.get("/auth/okta/callback", params={"code": "stolen-code", "state": state}).status_code == 502
