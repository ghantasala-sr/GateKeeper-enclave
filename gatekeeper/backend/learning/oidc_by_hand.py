"""Okta OIDC login, written by hand with only httpx and PyJWT. REFERENCE ONLY.

Flow: /auth/okta/login -> Okta -> /auth/okta/callback -> verified ID token claims.
The app uses app/auth/oidc.py (authlib). This file is not mounted;
it is kept to show what the library does for you, and its tests still run.
"""

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.config import settings

logger = logging.getLogger("gatekeeper.auth")
router = APIRouter(prefix="/auth/okta")

STATE_COOKIE = "gk_okta_state"
LOGIN_TTL_SECONDS = 600     # how long a user has to finish logging in at Okta
CLOCK_SKEW_SECONDS = 60     # tolerance for small clock differences with Okta
ALLOWED_ALGORITHMS = ["RS256"]  # never accept "none" or HS256 for ID tokens


class TokenValidationError(Exception):
    pass


# ---------- PKCE ----------

def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using the S256 method (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)  # 86 chars, within the 43-128 limit
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------- ID token validation ----------

def signing_key_for(id_token: str, jwks: dict):
    """Pick the JWKS key that signed this token, by the `kid` in its header."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.DecodeError as e:
        raise TokenValidationError(f"malformed token: {e}") from e
    if header.get("alg") not in ALLOWED_ALGORITHMS:
        raise TokenValidationError(f"unexpected alg {header.get('alg')!r}")
    for jwk in jwks.get("keys", []):
        if jwk.get("kid") == header.get("kid"):
            return jwt.PyJWK(jwk).key
    raise TokenValidationError(f"no JWKS key with kid {header.get('kid')!r}")


def validate_id_token(id_token: str, *, jwks: dict, issuer: str, client_id: str, nonce: str) -> dict:
    """Check signature, iss, aud, exp, iat, and nonce. Return the claims if all pass."""
    key = signing_key_for(id_token, jwks)
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=ALLOWED_ALGORITHMS,
            audience=client_id,
            issuer=issuer,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["iss", "aud", "exp", "iat", "sub", "nonce"]},
        )
    except jwt.PyJWTError as e:
        raise TokenValidationError(str(e)) from e

    # A token can carry several audiences; then azp must name us (OIDC Core 3.1.3.7).
    if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id:
        raise TokenValidationError("azp does not match client_id")
    if not secrets.compare_digest(claims["nonce"], nonce):
        raise TokenValidationError("nonce mismatch")
    return claims


# ---------- Okta endpoints (discovery + JWKS, cached) ----------

_discovery: dict | None = None
_jwks: dict | None = None


async def discovery() -> dict:
    global _discovery
    if _discovery is None:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(f"{settings.okta_issuer.rstrip('/')}/.well-known/openid-configuration")
            resp.raise_for_status()
            doc = resp.json()
        # The document must describe the issuer we were configured with.
        if doc["issuer"] != settings.okta_issuer.rstrip("/"):
            raise HTTPException(500, "OIDC discovery issuer does not match OKTA_ISSUER")
        _discovery = doc
    return _discovery


async def jwks(refresh: bool = False) -> dict:
    global _jwks
    if _jwks is None or refresh:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get((await discovery())["jwks_uri"])
            resp.raise_for_status()
            _jwks = resp.json()
    return _jwks


# ---------- Pending logins ----------

@dataclass
class PendingLogin:
    nonce: str
    code_verifier: str
    expires_at: float


# state -> PendingLogin. In-memory, so it only works with one server process.
_pending: dict[str, PendingLogin] = {}


def _require_config() -> None:
    if not (settings.okta_issuer and settings.okta_client_id and settings.okta_client_secret):
        raise HTTPException(503, "Okta is not configured; set OKTA_ISSUER, OKTA_CLIENT_ID, OKTA_CLIENT_SECRET in .env")


# ---------- Routes ----------

@router.get("/login")
async def login() -> RedirectResponse:
    _require_config()
    now = time.time()
    for s in [s for s, p in _pending.items() if p.expires_at < now]:
        del _pending[s]

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = make_pkce_pair()
    _pending[state] = PendingLogin(nonce, verifier, now + LOGIN_TTL_SECONDS)

    params = {
        "response_type": "code",
        "client_id": settings.okta_client_id,
        "redirect_uri": settings.okta_redirect_uri,
        "scope": "openid email profile",  # groups arrive via the ID token claim set up in Okta
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(f"{(await discovery())['authorization_endpoint']}?{urlencode(params)}")
    # Ties the callback to this browser, so an attacker can't finish a login they started.
    response.set_cookie(
        STATE_COOKIE, state, max_age=LOGIN_TTL_SECONDS, httponly=True,
        samesite="lax", secure=settings.okta_redirect_uri.startswith("https://"), path="/auth/okta",
    )
    return response


@router.get("/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None,
                   error: str | None = None, error_description: str | None = None) -> dict:
    _require_config()
    if error:
        raise HTTPException(400, f"Okta returned an error: {error}: {error_description or ''}")
    if not code or not state:
        raise HTTPException(400, "Missing code or state")

    cookie_state = request.cookies.get(STATE_COOKIE)
    if not cookie_state or not secrets.compare_digest(cookie_state, state):
        raise HTTPException(400, "State does not match this browser's login")
    pending = _pending.pop(state, None)  # pop: each state works only once
    if pending is None or pending.expires_at < time.time():
        raise HTTPException(400, "Login expired or already used; start again")

    # Exchange the one-time code (plus the PKCE verifier) for tokens.
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.post(
            (await discovery())["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.okta_redirect_uri,
                "code_verifier": pending.code_verifier,
            },
            auth=(settings.okta_client_id, settings.okta_client_secret),  # client_secret_basic
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        logger.warning("token exchange failed: %s %s", resp.status_code, resp.text[:500])
        raise HTTPException(502, "Token exchange with Okta failed")
    id_token = resp.json().get("id_token")
    if not id_token:
        raise HTTPException(502, "Okta did not return an ID token")

    args = dict(issuer=settings.okta_issuer.rstrip("/"), client_id=settings.okta_client_id, nonce=pending.nonce)
    try:
        try:
            claims = validate_id_token(id_token, jwks=await jwks(), **args)
        except TokenValidationError as e:
            if "no JWKS key" not in str(e):
                raise
            # Okta may have rotated its keys since we cached them; refetch once.
            claims = validate_id_token(id_token, jwks=await jwks(refresh=True), **args)
    except TokenValidationError as e:
        logger.warning("ID token rejected: %s", e)
        raise HTTPException(401, "ID token failed validation")

    logger.info("okta login verified: sub=%s email=%s groups=%s", claims["sub"], claims.get("email"), claims.get("groups"))
    # The real app turns these claims into a Principal (app/auth/bridge.py); this just shows them.
    return {"sub": claims["sub"], "email": claims.get("email"), "groups": claims.get("groups", [])}
