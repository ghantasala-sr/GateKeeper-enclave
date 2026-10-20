"""SCIM provisioning and instant lockout, against the real app and database.

Requests use the shapes Okta's "SCIM 2.0 Test App" sends. Each tenant gets a
throwaway SCIM token for the test; the real tokens are restored afterwards.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import main
from app.auth.bridge import ROLE_SCOPES, Principal
from app.auth.tokens import issue_access_token
from scripts.create_scim_token import hash_token
from tests.owner import owner, owner_plain
from tests.test_agent import FakeChatModel

SCIM = "application/scim+json"


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def tokens():
    """Swap in known SCIM tokens for both tenants; restore the real hashes after."""
    saved = {r["name"]: r["scim_token_hash"] for r in owner_plain("SELECT name, scim_token_hash FROM tenants", fetch=True)}
    tok = {name: f"test-scim-{name}-{uuid.uuid4()}" for name in saved}
    for name, t in tok.items():
        owner_plain("UPDATE tenants SET scim_token_hash = $1 WHERE name = $2", hash_token(t), name)
    yield tok
    for name, h in saved.items():
        owner_plain("UPDATE tenants SET scim_token_hash = $1 WHERE name = $2", h, name)


@pytest.fixture
def created():
    """Emails of users made during a test; deleted afterwards."""
    emails = []
    yield emails
    owner("DELETE FROM users WHERE email = ANY($1::text[])", emails)


def h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": SCIM, "Accept": SCIM}


def okta_user(email: str, active=True) -> dict:
    # What Okta POSTs when a user is assigned to the app.
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": email,
        "name": {"givenName": "Pat", "familyName": "Lee"},
        "emails": [{"primary": True, "value": email, "type": "work"}],
        "displayName": "Pat Lee",
        "locale": "en-US",
        "externalId": f"00u{uuid.uuid4().hex[:17]}",
        "groups": [],
        "password": "should-never-be-stored",
        "active": active,
    }


def create(client, token, created, email=None):
    email = email or f"scim-{uuid.uuid4().hex[:8]}@acme.example"
    created.append(email)
    resp = client.post("/scim/v2/Users", json=okta_user(email), headers=h(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


DEACTIVATE = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
              "Operations": [{"op": "replace", "value": {"active": False}}]}


# ---------- auth ----------

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "nope"}])
def test_bad_or_missing_token_is_401(client, tokens, headers):
    resp = client.get("/scim/v2/Users", headers=headers)
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith(SCIM)
    assert resp.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]


def test_user_access_token_is_not_a_scim_token(client, tokens):
    emp = owner("SELECT id, tenant_id FROM users WHERE email = 'admin@acme.example'", fetch=True)[0]
    user_token = issue_access_token(Principal(str(emp["id"]), str(emp["tenant_id"]), "a", "admin", ROLE_SCOPES["admin"]))
    assert client.get("/scim/v2/Users", headers=h(user_token)).status_code == 401


def test_raw_token_without_bearer_prefix_works(client, tokens):
    assert client.get("/scim/v2/Users", headers={"Authorization": tokens["Acme Corp"]}).status_code == 200


# ---------- create / read ----------

def test_create_user(client, tokens, created):
    user = create(client, tokens["Acme Corp"], created)
    assert user["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]
    assert user["active"] is True and user["name"] == {"givenName": "Pat", "familyName": "Lee"}
    assert "password" not in user
    [row] = owner("SELECT t.name, u.role, u.active, u.external_id FROM users u JOIN tenants t ON t.id = u.tenant_id "
                  "WHERE u.id = $1::uuid", user["id"], fetch=True)
    assert (row["name"], row["role"], row["active"]) == ("Acme Corp", "employee", True)
    assert row["external_id"] == user["externalId"]


def test_duplicate_is_409(client, tokens, created):
    user = create(client, tokens["Acme Corp"], created)
    resp = client.post("/scim/v2/Users", json=okta_user(user["userName"]), headers=h(tokens["Acme Corp"]))
    assert resp.status_code == 409 and resp.json()["scimType"] == "uniqueness"


def test_filter_by_username(client, tokens, created):
    user = create(client, tokens["Acme Corp"], created)
    resp = client.get("/scim/v2/Users", params={"filter": f'userName eq "{user["userName"].upper()}"'},
                      headers=h(tokens["Acme Corp"]))
    body = resp.json()
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    assert body["totalResults"] == 1 and body["Resources"][0]["id"] == user["id"]

    none = client.get("/scim/v2/Users", params={"filter": 'userName eq "nobody@acme.example"'},
                      headers=h(tokens["Acme Corp"])).json()
    assert none["totalResults"] == 0 and none["Resources"] == []


@pytest.mark.parametrize("bad_filter", ['userName sw "a"', 'email eq "x"', 'userName eq "x" or 1 eq 1', "1=1"])
def test_unsupported_filter_is_400(client, tokens, bad_filter):
    resp = client.get("/scim/v2/Users", params={"filter": bad_filter}, headers=h(tokens["Acme Corp"]))
    assert resp.status_code == 400 and resp.json()["scimType"] == "invalidFilter"


def test_get_user_and_404(client, tokens, created):
    user = create(client, tokens["Acme Corp"], created)
    assert client.get(f"/scim/v2/Users/{user['id']}", headers=h(tokens["Acme Corp"])).json()["id"] == user["id"]
    assert client.get(f"/scim/v2/Users/{uuid.uuid4()}", headers=h(tokens["Acme Corp"])).status_code == 404
    assert client.get("/scim/v2/Users/not-a-uuid", headers=h(tokens["Acme Corp"])).status_code == 404


# ---------- tenant isolation ----------

def test_each_tenants_token_sees_only_its_users(client, tokens):
    for tenant, domain in (("Acme Corp", "acme.example"), ("Globex", "globex.example")):
        body = client.get("/scim/v2/Users", headers=h(tokens[tenant])).json()
        assert body["totalResults"] >= 3
        assert all(r["userName"].endswith(domain) for r in body["Resources"])


def test_acme_token_cannot_read_or_change_globex_users(client, tokens):
    [globex_admin] = owner("SELECT id FROM users WHERE email = 'admin@globex.example'", fetch=True)
    gid = str(globex_admin["id"])
    acme = h(tokens["Acme Corp"])
    assert client.get(f"/scim/v2/Users/{gid}", headers=acme).status_code == 404
    assert client.patch(f"/scim/v2/Users/{gid}", json=DEACTIVATE, headers=acme).status_code == 404
    assert client.put(f"/scim/v2/Users/{gid}", json=okta_user("admin@globex.example", active=False),
                      headers=acme).status_code == 404
    assert owner("SELECT active FROM users WHERE id = $1", globex_admin["id"], fetch=True)[0]["active"] is True


def test_users_created_with_acme_token_land_in_acme(client, tokens, created):
    # Even if the body names a Globex address, the TOKEN decides the tenant.
    user = create(client, tokens["Acme Corp"], created, email=f"spy-{uuid.uuid4().hex[:6]}@globex.example")
    [row] = owner("SELECT t.name FROM users u JOIN tenants t ON t.id = u.tenant_id WHERE u.id = $1::uuid",
                  user["id"], fetch=True)
    assert row["name"] == "Acme Corp"


# ---------- update / deactivate ----------

def test_put_replaces_user(client, tokens, created):
    user = create(client, tokens["Acme Corp"], created)
    body = okta_user(user["userName"])
    body["name"] = {"givenName": "Patricia", "familyName": "Lee-Smith"}
    resp = client.put(f"/scim/v2/Users/{user['id']}", json=body, headers=h(tokens["Acme Corp"]))
    assert resp.status_code == 200 and resp.json()["name"]["givenName"] == "Patricia"


@pytest.mark.parametrize("patch", [
    DEACTIVATE,
    {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
     "Operations": [{"op": "replace", "path": "active", "value": False}]},
    {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
     "Operations": [{"op": "Replace", "path": "active", "value": "False"}]},
])
def test_patch_deactivates(client, tokens, created, patch):
    user = create(client, tokens["Acme Corp"], created)
    resp = client.patch(f"/scim/v2/Users/{user['id']}", json=patch, headers=h(tokens["Acme Corp"]))
    assert resp.status_code == 200 and resp.json()["active"] is False
    assert owner("SELECT active FROM users WHERE id = $1::uuid", user["id"], fetch=True)[0]["active"] is False


def test_patch_rejects_unknown_paths(client, tokens, created):
    user = create(client, tokens["Acme Corp"], created)
    for path in ("role", "tenant_id", "password"):
        resp = client.patch(f"/scim/v2/Users/{user['id']}", headers=h(tokens["Acme Corp"]), json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": path, "value": "admin"}]})
        assert resp.status_code == 400, path


# ---------- the firing test: deactivation blocks the next request ----------

def test_deactivation_blocks_chat_immediately(client, tokens, created, monkeypatch, tmp_path):
    async def fake_run_agent(principal, question, trace=None):
        return "ok", []
    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    monkeypatch.setattr(main, "get_model", FakeChatModel)  # /chat's "is a model configured?" check
    monkeypatch.setattr(main.trace_log, "LOG_FILE", tmp_path / "trace.jsonl")

    user = create(client, tokens["Acme Corp"], created)
    [row] = owner("SELECT tenant_id FROM users WHERE id = $1::uuid", user["id"], fetch=True)
    token = issue_access_token(Principal(user["id"], str(row["tenant_id"]), user["userName"], "employee",
                                         ROLE_SCOPES["employee"]))
    ask = lambda: client.post("/chat", json={"question": "PTO?"}, headers={"Authorization": f"Bearer {token}"})  # noqa: E731

    assert ask().status_code == 200                                 # create user -> chat works
    client.patch(f"/scim/v2/Users/{user['id']}", json=DEACTIVATE, headers=h(tokens["Acme Corp"]))
    resp = ask()                                                    # same, unexpired token
    assert resp.status_code == 401 and resp.json()["detail"] == "User is not active"


def test_groups_is_an_empty_list(client, tokens):
    body = client.get("/scim/v2/Groups", params={"startIndex": 1, "count": 100}, headers=h(tokens["Acme Corp"])).json()
    assert body["totalResults"] == 0 and body["Resources"] == []
    assert client.get("/scim/v2/Groups").status_code == 401
