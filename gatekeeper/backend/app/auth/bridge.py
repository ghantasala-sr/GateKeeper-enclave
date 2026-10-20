"""The auth bridge: turn any verified login into one standard Principal.

Okta (OIDC) and SAML IdPs (Keycloak, Microsoft Entra ID) describe users differently. Everything
after login only ever sees a Principal.
"""

from dataclasses import dataclass

import asyncpg

from app.db import set_tenant_context


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    email: str
    role: str                # employee | hr_manager | admin
    scopes: tuple[str, ...]  # e.g. ("docs:read",)


class AuthBridgeError(Exception):
    """The login was valid, but it doesn't map to a Gatekeeper user.

    tenant_id/user_id are filled in when known, so the refusal can be audited.
    """

    def __init__(self, message: str, tenant_id: str | None = None, user_id: str | None = None):
        super().__init__(message)
        self.tenant_id, self.user_id = tenant_id, user_id


ROLE_FROM_OKTA_GROUP = {
    "gk-employee": "employee",
    "gk-hr-manager": "hr_manager",
    "gk-admin": "admin",
}

# Lowest first. With several groups, the LOWEST role wins, so an extra or
# stale group can never quietly raise someone's access.
ROLE_ORDER = ("employee", "hr_manager", "admin")

ROLE_SCOPES = {
    "employee": ("docs:read",),
    "hr_manager": ("docs:read",),
    "admin": ("docs:read", "docs:admin"),
}


def lowest_role(roles: set[str]) -> str:
    if not roles:
        raise AuthBridgeError("user has no Gatekeeper role")
    return min(roles, key=ROLE_ORDER.index)


def role_from_groups(groups: list[str]) -> str:
    """Okta: gk- groups."""
    return lowest_role({ROLE_FROM_OKTA_GROUP[g] for g in groups if g in ROLE_FROM_OKTA_GROUP})


def role_from_app_roles(values: list[str]) -> str:
    """SAML: Entra app roles (or Keycloak roles) whose values are the role names."""
    return lowest_role({v for v in values if v in ROLE_ORDER})


# Where SAML IdPs put the email. Entra uses the long claim URI by default.
SAML_EMAIL_ATTRIBUTES = ("email", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress")
SAML_ROLE_ATTRIBUTES = ("role", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role")


async def principal_from_okta(conn: asyncpg.Connection, claims: dict) -> Principal:
    """Map verified Okta ID token claims to a Principal, creating or updating the user."""
    # `iss` was already checked against OKTA_ISSUER during token validation,
    # so it identifies which IdP sent this login.
    tenant_id = await conn.fetchval(
        "SELECT id FROM tenants WHERE idp_issuer = $1 AND idp_type = 'okta_oidc'", claims["iss"]
    )
    if tenant_id is None:
        raise AuthBridgeError(f"no tenant is configured for issuer {claims['iss']}")

    try:
        role = role_from_groups(claims.get("groups") or [])
        email = (claims.get("email") or "").strip().lower()
        if not email:
            raise AuthBridgeError("ID token has no email")
        return await _upsert_user(conn, tenant_id, external_id=claims["sub"], email=email, role=role)
    except AuthBridgeError as e:
        e.tenant_id = e.tenant_id or str(tenant_id)
        raise


async def principal_from_saml(conn: asyncpg.Connection, *, issuer: str, name_id: str,
                              attributes: dict[str, list[str]]) -> Principal:
    """Map a verified SAML assertion to a Principal, creating or updating the user."""
    # `issuer` was already checked against SAML_IDP_ENTITY_ID when the signature was validated.
    tenant_id = await conn.fetchval(
        "SELECT id FROM tenants WHERE idp_issuer = $1 AND idp_type = 'entra_saml'", issuer
    )
    if tenant_id is None:
        raise AuthBridgeError(f"no tenant is configured for issuer {issuer}")
    try:
        if not name_id:
            raise AuthBridgeError("assertion has no NameID")
        role = role_from_app_roles([v for a in SAML_ROLE_ATTRIBUTES for v in attributes.get(a, [])])
        emails = [v for a in SAML_EMAIL_ATTRIBUTES for v in attributes.get(a, [])]
        email = (emails[0] if emails else "").strip().lower()
        if not email:
            raise AuthBridgeError("assertion has no email")
        return await _upsert_user(conn, tenant_id, external_id=name_id, email=email, role=role)
    except AuthBridgeError as e:
        e.tenant_id = e.tenant_id or str(tenant_id)
        raise


async def _upsert_user(conn: asyncpg.Connection, tenant_id, *, external_id: str, email: str, role: str) -> Principal:
    async with conn.transaction():
        # `users` is under RLS: scope this transaction to the tenant the verified
        # IdP issuer resolved to, before touching any user row.
        await set_tenant_context(conn, str(tenant_id))
        user = await conn.fetchrow(
            "SELECT id, active FROM users WHERE tenant_id = $1 AND external_id = $2 FOR UPDATE",
            tenant_id, external_id,
        )
        if user is None:
            # First login. Adopt a pre-created row with this email (seeded now,
            # SCIM-provisioned), but never one already linked to someone else.
            existing = await conn.fetchrow(
                "SELECT id, active, external_id FROM users WHERE tenant_id = $1 AND lower(email) = $2 FOR UPDATE",
                tenant_id, email,
            )
            if existing is not None and existing["external_id"] is not None:
                raise AuthBridgeError("email already belongs to a different IdP user", user_id=str(existing["id"]))
            if existing is not None:
                user = existing
            else:
                user = await conn.fetchrow(
                    "INSERT INTO users (tenant_id, email, external_id, role) VALUES ($1, $2, $3, $4) RETURNING id, active",
                    tenant_id, email, external_id, role,
                )

        if not user["active"]:
            raise AuthBridgeError("user is deactivated", user_id=str(user["id"]))

        try:
            # The IdP is the source of truth: refresh email and role on every login.
            await conn.execute(
                "UPDATE users SET external_id = $2, email = $3, role = $4 WHERE id = $1",
                user["id"], external_id, email, role,
            )
        except asyncpg.UniqueViolationError:
            raise AuthBridgeError("email already belongs to another user") from None

    return Principal(
        user_id=str(user["id"]),
        tenant_id=str(tenant_id),
        email=email,
        role=role,
        scopes=ROLE_SCOPES[role],
    )
