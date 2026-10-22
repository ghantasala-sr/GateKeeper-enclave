"""Globex SAML login, tested against a fake IdP that signs its own assertions.

Each attack is a real, correctly-formed SAML Response with one thing wrong.
"""

import datetime as dt
import re
from base64 import b64encode
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient
from onelogin.saml2.constants import OneLogin_Saml2_Constants as C
from onelogin.saml2.utils import OneLogin_Saml2_Utils
from starlette.middleware.sessions import SessionMiddleware

from app.auth import saml
from app.auth.bridge import Principal

IDP = "https://fake-idp.example/globex"
SP = "urn:gatekeeper:globex"
ACS = "http://localhost:8000/auth/saml/acs"


def make_idp_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-idp")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30)).sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


IDP_KEY, IDP_CERT = make_idp_key()
ATTACKER_KEY, ATTACKER_CERT = make_idp_key()


def ts(minutes: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign(xml: str, key=IDP_KEY, cert=IDP_CERT) -> str:
    signed = OneLogin_Saml2_Utils.add_sign(xml, key, cert, sign_algorithm=C.RSA_SHA256, digest_algorithm=C.SHA256)
    return signed.decode() if isinstance(signed, bytes) else signed


def assertion(request_id: str, *, issuer=IDP, audience=SP, recipient=ACS, name_id="G-123",
              roles=("hr_manager",), email="hr.manager@globex.example", not_on_or_after=5, aid="_assert1") -> str:
    role_values = "".join(f"<saml:AttributeValue>{r}</saml:AttributeValue>" for r in roles)
    return f"""<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="{aid}" Version="2.0" IssueInstant="{ts()}"><saml:Issuer>{issuer}</saml:Issuer><saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:2.0:nameid-format:persistent">{name_id}</saml:NameID><saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData InResponseTo="{request_id}" NotOnOrAfter="{ts(not_on_or_after)}" Recipient="{recipient}"/></saml:SubjectConfirmation></saml:Subject><saml:Conditions NotBefore="{ts(-1)}" NotOnOrAfter="{ts(not_on_or_after)}"><saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction></saml:Conditions><saml:AuthnStatement AuthnInstant="{ts()}" SessionIndex="s1"><saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:unspecified</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement><saml:AttributeStatement><saml:Attribute Name="email"><saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute><saml:Attribute Name="role">{role_values}</saml:Attribute></saml:AttributeStatement></saml:Assertion>"""


def response(request_id: str, assertions: str, *, issuer=IDP, destination=ACS) -> str:
    return f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_resp1" Version="2.0" IssueInstant="{ts()}" Destination="{destination}" InResponseTo="{request_id}"><saml:Issuer>{issuer}</saml:Issuer><samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>{assertions}</samlp:Response>"""


def signed_response(request_id: str, *, key=IDP_KEY, cert=IDP_CERT, sign_assertion=True, sign_response=True,
                    response_kwargs=None, **assertion_kwargs) -> str:
    a = assertion(request_id, **assertion_kwargs)
    if sign_assertion:
        a = sign(a, key, cert)
    r = response(request_id, a, **(response_kwargs or {}))
    return sign(r, key, cert) if sign_response else r


@pytest.fixture
def sp(monkeypatch):
    monkeypatch.setattr(saml.settings, "saml_idp_metadata_url", "https://fake-idp.example/metadata")
    monkeypatch.setattr(saml.settings, "saml_idp_entity_id", IDP)
    monkeypatch.setattr(saml.settings, "saml_sp_entity_id", SP)
    monkeypatch.setattr(saml.settings, "saml_sp_acs_url", ACS)
    monkeypatch.setattr(saml, "_idp", {
        "entityId": IDP,
        "singleSignOnService": {"url": "https://fake-idp.example/sso",
                                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"},
        "x509cert": "".join(IDP_CERT.splitlines()[1:-1]),
    })

    seen = {}

    async def fake_principal_for(issuer, name_id, attributes):
        seen.update(issuer=issuer, name_id=name_id, attributes=attributes)
        return Principal("user-1", "tenant-globex", attributes["email"][0], "hr_manager", ("docs:read",))

    monkeypatch.setattr(saml, "principal_for", fake_principal_for)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-session-secret", session_cookie="gk_login")
    app.include_router(saml.router)
    client = TestClient(app, base_url="http://localhost:8000")
    client.seen = seen
    return client


def start_login(client) -> str:
    resp = client.get("/auth/saml/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://fake-idp.example/sso?")
    xml = OneLogin_Saml2_Utils.decode_base64_and_inflate(parse_qs(urlparse(resp.headers["location"]).query)["SAMLRequest"][0])
    xml = xml.decode() if isinstance(xml, bytes) else xml
    return re.search(r'ID="([^"]+)"', xml).group(1)


def post(client, xml: str):
    return client.post("/auth/saml/acs", data={"SAMLResponse": b64encode(xml.encode()).decode()})


# ---------- happy path ----------

def test_valid_login_returns_access_token(sp):
    resp = post(sp, signed_response(start_login(sp)))
    assert resp.status_code == 200, resp.text
    assert resp.json()["principal"]["email"] == "hr.manager@globex.example"
    assert resp.json()["token_type"] == "Bearer"
    assert sp.seen == {"issuer": IDP, "name_id": "G-123",
                       "attributes": {"email": ["hr.manager@globex.example"], "role": ["hr_manager"]}}


def test_login_works_behind_a_gateway_that_strips_a_prefix(sp, monkeypatch):
    """The browser posts to https://.../api/auth/saml/acs; we receive /auth/saml/acs over HTTP."""
    public_acs = "https://gatekeeper.example/api/auth/saml/acs"
    monkeypatch.setattr(saml.settings, "saml_sp_acs_url", public_acs)
    resp = post(sp, signed_response(start_login(sp), response_kwargs={"destination": public_acs},
                                    recipient=public_acs))
    assert resp.status_code == 200, resp.text


def test_metadata_is_valid(sp):
    resp = sp.get("/auth/saml/metadata")
    assert resp.status_code == 200
    assert f'entityID="{SP}"' in resp.text and ACS in resp.text


# ---------- attacks: each must be rejected and never reach the bridge ----------

ATTACKS = {
    "signed by another key": lambda rid: signed_response(rid, key=ATTACKER_KEY, cert=ATTACKER_CERT),
    "not signed at all": lambda rid: signed_response(rid, sign_assertion=False, sign_response=False),
    "response signed, assertion not": lambda rid: signed_response(rid, sign_assertion=False),
    "assertion signed, response not": lambda rid: signed_response(rid, sign_response=False),
    "meant for another SP (audience)": lambda rid: signed_response(rid, audience="urn:some-other-app"),
    "sent to another ACS (recipient)": lambda rid: signed_response(rid, recipient="https://evil.example/acs"),
    "sent to another ACS (destination)": lambda rid: signed_response(rid, response_kwargs={"destination": "https://evil.example/acs"}),
    "expired": lambda rid: signed_response(rid, not_on_or_after=-10),
    "answers a different login": lambda rid: signed_response("_someone-elses-request"),
    "issued by another IdP": lambda rid: signed_response(rid, issuer="https://evil-idp.example",
                                                         response_kwargs={"issuer": "https://evil-idp.example"}),
}


@pytest.mark.parametrize("attack", ATTACKS)
def test_attack_rejected(sp, attack):
    resp = post(sp, ATTACKS[attack](start_login(sp)))
    assert resp.status_code in (400, 401), f"{attack}: {resp.status_code} {resp.text}"
    assert not sp.seen, f"{attack}: reached the bridge"


def test_role_edited_after_signing_rejected(sp):
    rid = start_login(sp)
    tampered = signed_response(rid).replace(
        "<saml:AttributeValue>hr_manager</saml:AttributeValue>", "<saml:AttributeValue>admin</saml:AttributeValue>")
    assert "admin" in tampered
    assert post(sp, tampered).status_code == 401
    assert not sp.seen


def test_signature_wrapping_rejected(sp):
    # XSW: keep the genuine signed assertion, but slip an unsigned evil one in
    # front of it, hoping the app reads the evil one and checks the genuine one.
    rid = start_login(sp)
    genuine = sign(assertion(rid))
    evil = assertion(rid, name_id="G-evil", roles=("admin",), email="admin@globex.example", aid="_evil")
    resp = post(sp, sign(response(rid, evil + genuine)))
    assert resp.status_code == 401
    assert not sp.seen


def test_replay_rejected(sp):
    rid = start_login(sp)
    xml = signed_response(rid)
    assert post(sp, xml).status_code == 200
    sp.seen.clear()
    assert post(sp, xml).status_code == 400  # the login it answered is used up
    assert not sp.seen


def test_idp_initiated_login_rejected(sp):
    # A response with no login started by this browser (no session).
    xml = signed_response("_unsolicited")
    assert post(sp, xml).status_code == 400
    assert not sp.seen


def test_garbage_is_401_not_500(sp):
    start_login(sp)
    resp = sp.post("/auth/saml/acs", data={"SAMLResponse": "not-base64-xml"})
    assert resp.status_code == 401
