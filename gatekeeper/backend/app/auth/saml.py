"""Globex SAML login using python3-saml.

Flow: /auth/saml/login -> IdP (Keycloak locally, Microsoft Entra ID in real life)
-> browser POSTs a signed assertion to /auth/saml/acs -> Principal -> access token.

Only SP-initiated logins are accepted: the response must answer an AuthnRequest
this browser started (InResponseTo), and each request ID works once.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
from onelogin.saml2.settings import OneLogin_Saml2_Settings

from app import audit, db
from app.auth import frontend
from app.auth.bridge import AuthBridgeError, Principal, principal_from_saml
from app.auth.tokens import login_response
from app.config import settings

logger = logging.getLogger("gatekeeper.auth")
router = APIRouter(prefix="/auth/saml")

SESSION_KEY = "saml_request_id"

_idp: dict | None = None


def idp_settings() -> dict:
    """Load the IdP's signing cert and SSO URL from its metadata, once."""
    global _idp
    if not (settings.saml_idp_metadata_url and settings.saml_idp_entity_id):
        raise HTTPException(503, "SAML is not configured; set SAML_IDP_METADATA_URL and SAML_IDP_ENTITY_ID in .env")
    if _idp is None:
        parsed = OneLogin_Saml2_IdPMetadataParser.parse_remote(settings.saml_idp_metadata_url, timeout=10)
        # Metadata must describe the IdP we were configured to trust.
        if parsed["idp"]["entityId"] != settings.saml_idp_entity_id:
            raise HTTPException(500, "SAML metadata entity ID does not match SAML_IDP_ENTITY_ID")
        _idp = parsed["idp"]
    return _idp


def saml_settings() -> dict:
    return {
        "strict": True,  # reject anything not exactly as expected
        "debug": False,
        "sp": {
            "entityId": settings.saml_sp_entity_id,
            "assertionConsumerService": {
                "url": settings.saml_sp_acs_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
        },
        "idp": idp_settings(),
        "security": {
            "wantMessagesSigned": True,      # the whole <Response>
            "wantAssertionsSigned": True,    # and the <Assertion> inside it
            "wantNameId": True,
            "rejectUnsolicitedResponsesWithInResponseTo": True,
            "requestedAuthnContext": False,  # let the IdP choose how users authenticate (MFA etc.)
            "signatureAlgorithm": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
            "digestAlgorithm": "http://www.w3.org/2001/04/xmlenc#sha256",
            "rejectDeprecatedAlgorithm": True,  # no SHA-1
        },
    }


async def _request_data(request: Request) -> dict:
    """Describe the HTTP request the way python3-saml expects."""
    form = await request.form() if request.method == "POST" else {}
    return {
        "https": "on" if request.url.scheme == "https" else "off",
        "http_host": request.url.hostname,
        "server_port": request.url.port,
        "script_name": request.url.path,
        "get_data": dict(request.query_params),
        "post_data": dict(form),
    }


async def principal_for(issuer: str, name_id: str, attributes: dict) -> Principal:
    """Bridge a verified login to a Principal, and audit the outcome."""
    async with db.get_pool().acquire() as conn:
        try:
            principal = await principal_from_saml(conn, issuer=issuer, name_id=name_id, attributes=attributes)
        except AuthBridgeError as e:
            if e.tenant_id:  # unknown issuer: no tenant to log under (app log only)
                await audit.record_as_tenant(e.tenant_id, action="login_refused", user_id=e.user_id,
                                             detail=f"saml: {e}")
            raise
    await audit.record_as_tenant(principal.tenant_id, action="login", user_id=principal.user_id, detail="saml")
    return principal


@router.get("/metadata")
def metadata() -> Response:
    """Our SP metadata, for configuring the IdP side."""
    sp = OneLogin_Saml2_Settings(saml_settings(), sp_validation_only=True)
    xml = sp.get_sp_metadata()
    errors = sp.validate_metadata(xml)
    if errors:
        raise HTTPException(500, f"Invalid SP metadata: {errors}")
    return Response(xml, media_type="application/xml")


@router.get("/login")
async def login(request: Request, next: str | None = None) -> RedirectResponse:
    frontend.remember_next(request, next)
    auth = OneLogin_Saml2_Auth(await _request_data(request), saml_settings())
    url = auth.login()
    # Remember which AuthnRequest this browser sent; the response must answer it.
    request.session[SESSION_KEY] = auth.get_last_request_id()
    return RedirectResponse(url, status_code=302)


@router.post("/acs")
async def acs(request: Request):
    return await frontend.finish(request, lambda: _complete_login(request))


async def _complete_login(request: Request) -> dict:
    # pop: each AuthnRequest can be answered once, so a captured response can't be replayed.
    request_id = request.session.pop(SESSION_KEY, None)
    if request_id is None:
        raise HTTPException(400, "No SAML login in progress for this browser (IdP-initiated logins are not accepted)")

    auth = OneLogin_Saml2_Auth(await _request_data(request), saml_settings())
    try:
        auth.process_response(request_id=request_id)
    except Exception as e:  # noqa: BLE001
        # Anything raised while parsing untrusted input is a failed login, not a 500:
        # malformed base64/XML (lxml), duplicated attributes, missing SAMLResponse, ...
        logger.warning("SAML response rejected: %r", e)
        raise HTTPException(401, "SAML response failed validation")
    errors = auth.get_errors()
    if errors or not auth.is_authenticated():
        # Bad signature, wrong audience/issuer/destination, expired, wrong InResponseTo, ...
        logger.warning("SAML response rejected: %s %s", errors, auth.get_last_error_reason())
        raise HTTPException(401, "SAML response failed validation")

    name_id, attributes = auth.get_nameid(), auth.get_attributes()
    try:
        principal = await principal_for(settings.saml_idp_entity_id, name_id, attributes)
    except AuthBridgeError as e:
        logger.warning("SAML login refused: name_id=%s attributes=%s: %s", name_id, attributes, e)
        raise HTTPException(403, f"Login refused: {e}")

    logger.info("saml login: user=%s tenant=%s role=%s", principal.user_id, principal.tenant_id, principal.role)
    return login_response(principal)
