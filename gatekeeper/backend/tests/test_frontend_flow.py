"""What the frontend relies on — login redirects, /me, and CORS."""

import re
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from onelogin.saml2.utils import OneLogin_Saml2_Utils

from app import main
from app.auth.tokens import issue_access_token, verify_access_token
from tests.test_oidc import BAD_ID_TOKENS, finish_login, okta  # noqa: F401 (fixture)
from tests.test_oidc import start_login as start_okta_login
from tests.test_saml import post as saml_post
from tests.test_saml import signed_response, sp  # noqa: F401 (fixture)
from tests.test_tokens import acme_user  # noqa: F401 (fixture)

FRONTEND = "http://localhost:3000"


def fragment(resp) -> dict:
    assert resp.status_code == 303, resp.text
    url = urlparse(resp.headers["location"])
    assert f"{url.scheme}://{url.netloc}" == FRONTEND and url.path == "/" and not url.query
    return {k: v[0] for k, v in parse_qs(url.fragment).items()}


@pytest.fixture(autouse=True)
def frontend_url(monkeypatch):
    monkeypatch.setattr(main.settings, "frontend_url", FRONTEND)


def app_okta_login(client, next_value="app"):
    """Like the frontend's button (/auth/okta/login?next=app), then finish at the callback."""
    resp = client.get("/auth/okta/login", params={"next": next_value}, follow_redirects=False)
    q = parse_qs(urlparse(resp.headers["location"]).query)
    client.fake["challenge"], client.fake["nonce"] = q["code_challenge"][0], q["nonce"][0]
    client.follow_redirects = False  # inspect the redirect to the frontend, don't follow it
    return finish_login(client, q["state"][0])


# ---------- Okta ----------

def test_okta_login_from_app_redirects_with_token_in_fragment(okta):  # noqa: F811
    frag = fragment(app_okta_login(okta))
    assert frag["expires_in"] == "900"
    assert verify_access_token(frag["access_token"])["role"] == "hr_manager"


def test_okta_login_without_next_still_returns_json(okta):  # noqa: F811
    resp = finish_login(okta, start_okta_login(okta))
    assert resp.status_code == 200 and "access_token" in resp.json()


def test_okta_refusal_from_app_redirects_with_error(okta):  # noqa: F811
    okta.fake["bridge_error"] = "user has no Gatekeeper role"
    frag = fragment(app_okta_login(okta))
    assert "access_token" not in frag and "no Gatekeeper role" in frag["error"]


def test_bad_token_from_app_redirects_with_error_not_token(okta):  # noqa: F811
    okta.fake["make_id_token"] = BAD_ID_TOKENS["forged signature"]
    frag = fragment(app_okta_login(okta))
    assert frag == {"error": "ID token failed validation"}


def test_next_only_accepts_app(okta):  # noqa: F811
    # No open redirect: anything but next=app falls back to JSON; the target is always FRONTEND_URL.
    assert app_okta_login(okta, next_value="https://evil.example").status_code == 200


# ---------- SAML ----------

def test_saml_login_from_app_redirects_with_token(sp):  # noqa: F811
    resp = sp.get("/auth/saml/login", params={"next": "app"}, follow_redirects=False)
    xml = OneLogin_Saml2_Utils.decode_base64_and_inflate(parse_qs(urlparse(resp.headers["location"]).query)["SAMLRequest"][0])
    request_id = re.search(r'ID="([^"]+)"', xml.decode() if isinstance(xml, bytes) else xml).group(1)
    sp.follow_redirects = False
    frag = fragment(saml_post(sp, signed_response(request_id)))
    assert verify_access_token(frag["access_token"])["tenant_id"] == "tenant-globex"


# ---------- /me and CORS (real app) ----------

@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def test_me_returns_badge_info(client, acme_user):  # noqa: F811
    p = acme_user("hr_manager")
    resp = client.get("/me", headers={"Authorization": f"Bearer {issue_access_token(p)}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"].endswith("@acme.example")  # from the database row, not the token
    assert {k: body[k] for k in ("role", "scopes", "tenant_id", "tenant", "idp_type")} == {
        "role": "hr_manager", "scopes": ["docs:read"], "tenant_id": p.tenant_id, "tenant": "Acme Corp",
        "idp_type": "okta_oidc"}
    assert client.get("/me").status_code == 401


def test_cors_allows_the_frontend_only(client):
    preflight = {"Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "authorization,content-type"}
    ok = client.options("/chat", headers={"Origin": FRONTEND, **preflight})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == FRONTEND
    assert "access-control-allow-credentials" not in ok.headers
    evil = client.options("/chat", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in evil.headers
