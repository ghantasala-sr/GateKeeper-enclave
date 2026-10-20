"""Gatekeeper's own access tokens.

After login, the browser gets a short-lived RS256 JWT that we sign ourselves.
Every protected request presents it as `Authorization: Bearer <token>`.
The token looks the same whichever IdP the user logged in through.
"""

import base64
import hashlib
import json
import time
import uuid
from dataclasses import asdict
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives import serialization
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import audit, db
from app.auth.bridge import Principal
from app.config import settings
from app.tracing import RequestTrace

ALGORITHM = "RS256"

_bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def _keys():
    """Load the private key once and derive its public JWK and key ID."""
    private_key = serialization.load_pem_private_key(base64.b64decode(settings.token_signing_key_b64), password=None)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    # Key ID = RFC 7638 thumbprint, so it changes automatically when the key does.
    thumbprint_input = json.dumps({k: public_jwk[k] for k in ("e", "kty", "n")}, separators=(",", ":"), sort_keys=True)
    kid = base64.urlsafe_b64encode(hashlib.sha256(thumbprint_input.encode()).digest()).rstrip(b"=").decode()
    public_jwk.update(kid=kid, alg=ALGORITHM, use="sig")
    return private_key, public_jwk


def jwks() -> dict:
    return {"keys": [_keys()[1]]}


def issue_access_token(principal: Principal) -> str:
    private_key, public_jwk = _keys()
    now = int(time.time())
    claims = {
        "iss": settings.token_issuer,
        "aud": settings.token_audience,
        "sub": principal.user_id,
        "tenant_id": principal.tenant_id,
        "role": principal.role,
        "scope": " ".join(principal.scopes),
        "iat": now,
        "exp": now + settings.token_ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_key, algorithm=ALGORITHM, headers={"kid": public_jwk["kid"]})


def login_response(principal: Principal) -> dict:
    """What every login (OIDC or SAML) returns as JSON. Frontend logins get it via auth/frontend.py."""
    return {
        "access_token": issue_access_token(principal),
        "token_type": "Bearer",
        "expires_in": settings.token_ttl_seconds,
        "principal": asdict(principal),
    }


def _unauthorized(detail: str) -> HTTPException:
    # 401 = "I don't know who you are."
    return HTTPException(401, detail, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


def verify_access_token(token: str) -> dict:
    private_key, public_jwk = _keys()
    try:
        if jwt.get_unverified_header(token).get("kid") != public_jwk["kid"]:
            raise _unauthorized("Token was not signed by this server's current key")
        return jwt.decode(
            token,
            private_key.public_key(),
            algorithms=[ALGORITHM],
            audience=settings.token_audience,
            issuer=settings.token_issuer,
            options={"require": ["iss", "aud", "sub", "exp", "iat", "tenant_id", "role", "scope"]},
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Token expired")
    except jwt.PyJWTError:
        raise _unauthorized("Invalid token")


def require_scope(scope: str):
    """FastAPI dependency: a valid token for an active user that carries `scope`."""

    async def dependency(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
        trace = request.state.trace = RequestTrace()
        if creds is None:
            raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        with trace.step("Access token verified", "auth") as t:
            claims = verify_access_token(creds.credentials)
            t["details"] = {
                "algorithm": ALGORITHM, "key_id": jwt.get_unverified_header(creds.credentials)["kid"][:12] + "…",
                "issuer": claims["iss"], "audience": claims["aud"],
                "expires_in_s": int(claims["exp"] - time.time()),
                "claims": {"sub": claims["sub"], "tenant_id": claims["tenant_id"], "role": claims["role"],
                           "scope": claims["scope"]},
                "checks": "signature (JWKS key), iss, aud, exp, required claims",
            }

        # Check the database on EVERY request. A valid signature only proves the
        # token was true 15 minutes ago; this catches deactivation immediately.
        # `users` is under RLS, so scope the lookup to the token's (signed) tenant first.
        with trace.step("User still active in the database", "database") as t:
            async with db.run_as_tenant(claims["tenant_id"]) as conn:
                user = await conn.fetchrow(
                    "SELECT email, role, active FROM users WHERE id = $1::uuid AND tenant_id = $2::uuid",
                    claims["sub"], claims["tenant_id"],
                )
            t["details"] = {
                "query": "SELECT email, role, active FROM users WHERE id = $1 (under RLS: own tenant only)",
                "found": user is not None,
                "active": bool(user and user["active"]),
                "role_matches_token": bool(user and user["role"] == claims["role"]),
                "why": "Catches IdP deactivation (SCIM) on the very next request, not when the token expires.",
            }
            t["ok"] = bool(user and user["active"] and user["role"] == claims["role"])
        scopes = tuple(claims["scope"].split())
        if user is None or not user["active"]:
            denial = _unauthorized("User is not active")
        elif user["role"] != claims["role"]:
            # Role changed since login (e.g. demoted). Make them log in again.
            denial = _unauthorized("Role changed; log in again")
        elif scope not in scopes:
            # 403 = "I know who you are, and the answer is no."
            denial = HTTPException(403, f"Missing scope {scope}",
                                   headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'})
        else:
            denial = None
        if denial is not None:
            # The token's signature was valid, so its tenant is trustworthy enough to log under.
            await audit.record_as_tenant(claims["tenant_id"], action="request_denied", user_id=claims["sub"],
                                         detail=f"{denial.status_code} {denial.detail}")
            raise denial
        return Principal(
            user_id=claims["sub"], tenant_id=claims["tenant_id"], email=user["email"],
            role=claims["role"], scopes=scopes,
        )

    return dependency

