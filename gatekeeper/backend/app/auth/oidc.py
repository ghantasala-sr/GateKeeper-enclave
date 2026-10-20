"""Okta OIDC login using authlib.

Same flow and checks as learning/oidc_by_hand.py, but authlib handles PKCE,
state, nonce, discovery, JWKS (with refetch on key rotation), and ID token
validation. The state/nonce/PKCE verifier live in a signed session cookie
set by SessionMiddleware (see app/main.py).
"""

import logging

from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from joserfc.errors import JoseError

from app import audit, db
from app.auth import frontend
from app.auth.bridge import AuthBridgeError, Principal, principal_from_okta
from app.auth.tokens import login_response
from app.config import settings

logger = logging.getLogger("gatekeeper.auth")
router = APIRouter(prefix="/auth/okta")

# Extra httpx client options; tests swap in a fake Okta transport here.
CLIENT_KWARGS: dict = {}

_okta = None


def okta():
    """Register the Okta client on first use, from the current settings."""
    global _okta
    if not (settings.okta_issuer and settings.okta_client_id and settings.okta_client_secret):
        raise HTTPException(503, "Okta is not configured; set OKTA_ISSUER, OKTA_CLIENT_ID, OKTA_CLIENT_SECRET in .env")
    if _okta is None:
        oauth = OAuth()
        _okta = oauth.register(
            name="okta",
            client_id=settings.okta_client_id,
            client_secret=settings.okta_client_secret,
            server_metadata_url=f"{settings.okta_issuer.rstrip('/')}/.well-known/openid-configuration",
            client_kwargs={
                "scope": "openid email profile",  # groups arrive via the ID token claim set up in Okta
                "code_challenge_method": "S256",  # PKCE
                "token_endpoint_auth_method": "client_secret_basic",
                **CLIENT_KWARGS,
            },
        )
    return _okta


async def principal_for(claims: dict) -> Principal:
    """Bridge a verified login to a Principal, and audit the outcome."""
    async with db.get_pool().acquire() as conn:
        try:
            principal = await principal_from_okta(conn, claims)
        except AuthBridgeError as e:
            if e.tenant_id:  # unknown issuer: no tenant to log under (app log only)
                await audit.record_as_tenant(e.tenant_id, action="login_refused", user_id=e.user_id,
                                             detail=f"okta_oidc: {e}")
            raise
    await audit.record_as_tenant(principal.tenant_id, action="login", user_id=principal.user_id, detail="okta_oidc")
    return principal


@router.get("/login")
async def login(request: Request, next: str | None = None):
    frontend.remember_next(request, next)
    return await okta().authorize_redirect(request, settings.okta_redirect_uri)


@router.get("/callback")
async def callback(request: Request):
    return await frontend.finish(request, lambda: _complete_login(request))


async def _complete_login(request: Request) -> dict:
    client = okta()
    try:
        token = await client.authorize_access_token(
            request,
            # Pin the issuer to our config. Without this, authlib trusts
            # whatever issuer the discovery document claims.
            claims_options={"iss": {"essential": True, "value": settings.okta_issuer.rstrip("/")}},
            leeway=60,
        )
    except OAuthError as e:
        # Okta-reported errors, bad/reused state, failed code exchange.
        logger.warning("okta login failed: %s %s", e.error, e.description)
        raise HTTPException(400, f"Login failed: {e.error or ''} {e.description or ''}".strip())
    except JoseError as e:
        # Bad signature, wrong iss/aud, expired, nonce mismatch, disallowed alg.
        logger.warning("ID token rejected: %r", e)
        raise HTTPException(401, "ID token failed validation")

    # authlib only validates an ID token if one came back, so insist on it.
    claims = token.get("userinfo")
    if not claims:
        raise HTTPException(502, "Okta did not return an ID token")

    try:
        principal = await principal_for(dict(claims))
    except AuthBridgeError as e:
        logger.warning("okta login refused: sub=%s email=%s groups=%s: %s", claims["sub"], claims.get("email"), claims.get("groups"), e)
        raise HTTPException(403, f"Login refused: {e}")

    logger.info("okta login: user=%s tenant=%s role=%s", principal.user_id, principal.tenant_id, principal.role)
    return login_response(principal)
