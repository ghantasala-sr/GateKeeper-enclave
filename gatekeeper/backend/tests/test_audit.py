"""The audit log records logins (and refusals), chats, denials, and SCIM changes,
and the app can only ever add to it."""

import asyncio
import uuid

import asyncpg
import pytest

from app import db
from app.auth import oidc
from app.auth.bridge import ROLE_SCOPES, AuthBridgeError, Principal
from app.auth.tokens import issue_access_token
from app.config import settings
from tests.test_agent import use_model
from tests.owner import owner, owner_plain
from tests.test_injection import INJECTIONS, client, compromised_model, env  # noqa: F401 (fixtures)
from tests.test_scim import DEACTIVATE, create, created, h, tokens  # noqa: F401 (fixtures)


def last_entries(n=5, **where):
    clauses = " AND ".join(f"{k} = ${i + 1}" for i, k in enumerate(where)) or "true"
    return owner_plain(f"SELECT * FROM audit_log WHERE {clauses} ORDER BY id DESC LIMIT {n}", *where.values(), fetch=True)


def tenant_id(name: str) -> uuid.UUID:
    return owner_plain("SELECT id FROM tenants WHERE name = $1", name, fetch=True)[0]["id"]


# ---------- the app can only append ----------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM audit_log",
    "UPDATE audit_log SET question = 'nothing to see here'",
    "DELETE FROM audit_log",
    "TRUNCATE audit_log",
    "INSERT INTO audit_log (tenant_id, action) VALUES (gen_random_uuid(), 'x') RETURNING id",  # RETURNING needs SELECT
])
def test_app_role_cannot_read_or_change_the_log(sql):
    async def run():
        conn = await asyncpg.connect(settings.app_database_url)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        asyncio.run(run())


def test_app_cannot_write_another_tenants_log():
    acme, globex = tenant_id("Acme Corp"), tenant_id("Globex")

    async def run():
        conn = await asyncpg.connect(settings.app_database_url)
        try:
            async with conn.transaction():
                await db.set_tenant_context(conn, str(acme))
                await conn.execute("INSERT INTO audit_log (tenant_id, action) VALUES ($1, 'forged')", globex)
        finally:
            await conn.close()
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        asyncio.run(run())


# ---------- the injection attempt is on record, with no Globex data ----------

def test_injection_chat_is_audited_with_only_acme_chunks(client, env, monkeypatch):  # noqa: F811
    use_model(monkeypatch, compromised_model(env["globex"]))
    question = INJECTIONS[0]
    resp = client.post("/chat", json={"question": question}, headers={"Authorization": f"Bearer {env['token']}"})
    assert resp.status_code == 200

    [entry] = last_entries(1, action="chat", user_id=uuid.UUID(env["employee"].user_id))
    assert entry["question"] == question
    assert str(entry["tenant_id"]) == env["employee"].tenant_id
    assert entry["chunk_ids"], "the chunks the agent received are on record"
    chunks = owner("SELECT t.name, c.classification FROM chunks c JOIN tenants t ON t.id = c.tenant_id "
                   "WHERE c.id = ANY($1::uuid[])", entry["chunk_ids"], fetch=True)
    assert len(chunks) == len(entry["chunk_ids"])
    assert {(r["name"], r["classification"]) for r in chunks} == {("Acme Corp", "public")}


# ---------- logins ----------

def test_successful_login_is_audited(client):  # noqa: F811
    if not settings.okta_issuer:
        pytest.skip("OKTA_ISSUER not set")
    [emp] = owner("SELECT id, external_id FROM users WHERE email = 'employee@acme.example'", fetch=True)
    claims = {"iss": settings.okta_issuer.rstrip("/"), "sub": emp["external_id"] or "00uTESTEMP",
              "email": "employee@acme.example", "groups": ["gk-employee"]}
    p = client.portal.call(oidc.principal_for, claims)
    [entry] = last_entries(1, action="login", user_id=uuid.UUID(p.user_id))
    assert entry["detail"] == "okta_oidc"


def test_refused_login_is_audited_without_a_user(client):  # noqa: F811
    if not settings.okta_issuer:
        pytest.skip("OKTA_ISSUER not set")
    claims = {"iss": settings.okta_issuer.rstrip("/"), "sub": f"00u{uuid.uuid4().hex[:17]}",
              "email": "intruder@acme.example", "groups": ["Everyone"]}
    with pytest.raises(AuthBridgeError):
        client.portal.call(oidc.principal_for, claims)
    [entry] = last_entries(1, action="login_refused")
    assert entry["user_id"] is None
    assert entry["tenant_id"] == tenant_id("Acme Corp")
    assert "no Gatekeeper role" in entry["detail"]
    # ...and no user was created for them.
    assert not owner("SELECT 1 FROM users WHERE email = 'intruder@acme.example'", fetch=True)


# ---------- SCIM and the firing test ----------

def test_scim_changes_and_the_blocked_request_are_audited(client, tokens, created, monkeypatch):  # noqa: F811
    user = create(client, tokens["Acme Corp"], created)
    uid = uuid.UUID(user["id"])
    assert last_entries(1, user_id=uid)[0]["action"] == "scim_create"

    client.patch(f"/scim/v2/Users/{user['id']}", json=DEACTIVATE, headers=h(tokens["Acme Corp"]))
    entry = last_entries(1, user_id=uid)[0]
    assert (entry["action"], entry["detail"]) == ("scim_deactivate", "changed: active")

    # The fired user tries again with their old token.
    token = issue_access_token(Principal(user["id"], str(tenant_id("Acme Corp")), user["userName"], "employee",
                                         ROLE_SCOPES["employee"]))
    assert client.post("/chat", json={"question": "hi"}, headers={"Authorization": f"Bearer {token}"}).status_code == 401
    entry = last_entries(1, user_id=uid)[0]
    assert (entry["action"], entry["detail"]) == ("request_denied", "401 User is not active")
