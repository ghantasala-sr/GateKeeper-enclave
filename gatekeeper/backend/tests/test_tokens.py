"""Gatekeeper access tokens and scopes, against the real app and database.

Needs Postgres running and seeded. The chat model is replaced with a stub.
"""

import base64
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import main
from app.auth import tokens
from app.auth.bridge import ROLE_SCOPES, Principal
from app.config import settings
from tests.owner import owner, owner_plain


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:  # runs lifespan: DB pool + embedding model
        yield c


@pytest.fixture(autouse=True)
def stub_chat_model(monkeypatch, tmp_path):
    monkeypatch.setattr(main.trace_log, "LOG_FILE", tmp_path / "trace.jsonl")  # keep the demo trace clean

    async def fake_run_agent(principal, question, trace=None):
        return "stub answer", []
    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    monkeypatch.setattr(main.settings, "anthropic_api_key", "stub")


@pytest.fixture
def acme_user(client):
    """A temporary Acme user per role; deleted afterwards."""
    created = []

    def make(role: str) -> Principal:
        tenant_id = str(owner_plain("SELECT id FROM tenants WHERE name = 'Acme Corp'", fetch=True)[0]["id"])
        [row] = owner("INSERT INTO users (tenant_id, email, role) VALUES ($1::uuid, $2, $3) RETURNING id",
                      tenant_id, f"test-{uuid.uuid4()}@acme.example", role, tenant=tenant_id, fetch=True)
        user_id = str(row["id"])
        created.append(user_id)
        return Principal(user_id, tenant_id, "test@acme.example", role, ROLE_SCOPES[role])

    yield make

    db(client, "DELETE FROM users WHERE id = ANY($1::uuid[])", created)


def db(client, sql: str, *args):
    """Run setup/teardown SQL on `users` as the owner, in every tenant's context
    (the app role can't delete users, and the owner doesn't bypass RLS on Cloud SQL)."""
    return owner(sql, *args)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def chat(client, headers=None):
    return client.post("/chat", json={"question": "What is the PTO policy?"}, headers=headers or {})


def tamper(token: str, **changes) -> str:
    """Change claims but keep the original signature."""
    header, payload, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims.update(changes)
    new_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{new_payload}.{sig}"


def sign_with(key, claims: dict, kid: str | None = None) -> str:
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid or tokens.jwks()["keys"][0]["kid"]})


# ---------- 401 vs 403: who you are vs what you may do ----------

def test_no_token_is_401(client):
    resp = chat(client)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")


def test_valid_token_can_chat(client, acme_user):
    resp = chat(client, bearer(tokens.issue_access_token(acme_user("employee"))))
    assert resp.status_code == 200, resp.text
    assert resp.json()["answer"] == "stub answer"


def test_expired_token_is_401(client, acme_user, monkeypatch):
    monkeypatch.setattr(tokens.settings, "token_ttl_seconds", -120)
    resp = chat(client, bearer(tokens.issue_access_token(acme_user("employee"))))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Token expired"


@pytest.mark.parametrize("changes", [
    {"role": "admin", "scope": "docs:read docs:admin"},  # self-promotion
    {"tenant_id": "00000000-0000-0000-0000-000000000000"},  # hop to another tenant
    {"exp": int(time.time()) + 86400 * 365},               # extend lifetime
])
def test_edited_token_is_401(client, acme_user, changes):
    token = tokens.issue_access_token(acme_user("employee"))
    assert chat(client, bearer(tamper(token, **changes))).status_code == 401


def test_token_signed_by_another_key_is_401(client, acme_user):
    p = acme_user("admin")
    claims = jwt.decode(tokens.issue_access_token(p), options={"verify_signature": False})
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert client.get("/admin/stats", headers=bearer(sign_with(attacker_key, claims))).status_code == 401


def test_okta_id_token_is_not_an_access_token(client, acme_user):
    # Right signature, wrong audience: a token meant for something else.
    p = acme_user("employee")
    claims = jwt.decode(tokens.issue_access_token(p), options={"verify_signature": False})
    claims["aud"] = settings.okta_client_id or "some-other-app"
    private_key, _ = tokens._keys()
    assert chat(client, bearer(sign_with(private_key, claims))).status_code == 401


def test_employee_calling_admin_stats_is_403(client, acme_user):
    resp = client.get("/admin/stats", headers=bearer(tokens.issue_access_token(acme_user("employee"))))
    assert resp.status_code == 403
    assert 'insufficient_scope' in resp.headers["www-authenticate"]


def test_hr_manager_calling_admin_stats_is_403(client, acme_user):
    resp = client.get("/admin/stats", headers=bearer(tokens.issue_access_token(acme_user("hr_manager"))))
    assert resp.status_code == 403


def test_admin_calling_admin_stats_is_200(client, acme_user):
    p = acme_user("admin")
    resp = client.get("/admin/stats", headers=bearer(tokens.issue_access_token(p)))
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == p.tenant_id
    assert resp.json()["documents"] == 10


# ---------- the database check on every request ----------

def test_deactivated_user_is_blocked_immediately(client, acme_user):
    p = acme_user("employee")
    token = tokens.issue_access_token(p)
    assert chat(client, bearer(token)).status_code == 200
    db(client, "UPDATE users SET active = false WHERE id = $1::uuid", p.user_id)
    resp = chat(client, bearer(token))  # same, still-unexpired token
    assert resp.status_code == 401
    assert resp.json()["detail"] == "User is not active"


def test_demoted_user_must_log_in_again(client, acme_user):
    p = acme_user("admin")
    token = tokens.issue_access_token(p)
    db(client, "UPDATE users SET role = 'employee' WHERE id = $1::uuid", p.user_id)
    assert client.get("/admin/stats", headers=bearer(token)).status_code == 401


def test_deleted_user_is_401(client, acme_user):
    p = acme_user("employee")
    token = tokens.issue_access_token(p)
    db(client, "DELETE FROM users WHERE id = $1::uuid", p.user_id)
    assert chat(client, bearer(token)).status_code == 401


# ---------- JWKS ----------

def test_jwks_publishes_only_the_public_key(client, acme_user):
    keys = client.get("/.well-known/jwks.json").json()["keys"]
    assert len(keys) == 1
    assert {"kty", "n", "e", "kid"} <= keys[0].keys()
    assert not {"d", "p", "q", "dp", "dq", "qi"} & keys[0].keys()  # no private parts

    # Anyone can verify our tokens with just the published key.
    token = tokens.issue_access_token(acme_user("employee"))
    claims = jwt.decode(token, jwt.PyJWK(keys[0]).key, algorithms=["RS256"],
                        audience=settings.token_audience, issuer=settings.token_issuer)
    assert claims["scope"] == "docs:read"
