"""The auth bridge, against the real database.

Needs Postgres running and seeded (docker compose up; python -m scripts.seed).
The bridge runs as the limited gatekeeper_app role, like the real app, inside a
transaction that is rolled back. The owner only creates and removes the test tenant.
"""

import asyncio
import uuid

import asyncpg
import pytest

from app.auth.bridge import AuthBridgeError, principal_from_okta, role_from_groups
from app.config import settings
from tests.owner import tenant_tx

ISSUER = "https://test-okta.example/oauth2/default"


@pytest.mark.parametrize(
    "groups, role",
    [
        (["gk-employee"], "employee"),
        (["gk-hr-manager"], "hr_manager"),
        (["gk-admin"], "admin"),
        (["gk-admin", "gk-employee"], "employee"),  # lowest role wins
        (["gk-hr-manager", "Everyone", "gk-admin"], "hr_manager"),
    ],
)
def test_role_from_groups(groups, role):
    assert role_from_groups(groups) == role


@pytest.mark.parametrize("groups", [[], ["Everyone"], ["gk-superuser"], ["GK-ADMIN"]])
def test_no_gatekeeper_group_is_refused(groups):
    with pytest.raises(AuthBridgeError):
        role_from_groups(groups)


def run_as_app(test, *, idp_type: str, issuer: str):
    """Owner creates a test tenant; the test runs as gatekeeper_app and is rolled back."""
    async def main():
        owner = await asyncpg.connect(settings.migration_database_url)
        tenant_id = await owner.fetchval(
            "INSERT INTO tenants (name, idp_type, idp_issuer) VALUES ($1, $2, $3) RETURNING id",
            f"Test Tenant {uuid.uuid4()}", idp_type, issuer,
        )
        app = await asyncpg.connect(settings.app_database_url)
        tx = app.transaction()
        await tx.start()
        try:
            await test(app, str(tenant_id), owner)
        finally:
            await tx.rollback()
            await app.close()
            async with tenant_tx(owner, tenant_id):
                await owner.execute("DELETE FROM users WHERE tenant_id = $1", tenant_id)
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
            await owner.close()
    asyncio.run(main())


def run_in_rollback(test):
    run_as_app(test, idp_type="okta_oidc", issuer=ISSUER)


def claims(**overrides) -> dict:
    return {"iss": ISSUER, "sub": "00uTEST", "email": "pat@test.example", "groups": ["gk-hr-manager"], **overrides}


def test_first_login_creates_user():
    async def test(conn, tenant_id, owner):
        p = await principal_from_okta(conn, claims())
        assert (p.tenant_id, p.email, p.role, p.scopes) == (tenant_id, "pat@test.example", "hr_manager", ("docs:read",))
        row = await conn.fetchrow("SELECT tenant_id, external_id, role FROM users WHERE id = $1", uuid.UUID(p.user_id))
        assert (str(row["tenant_id"]), row["external_id"], row["role"]) == (tenant_id, "00uTEST", "hr_manager")
    run_in_rollback(test)


def test_second_login_reuses_user_and_refreshes_role():
    async def test(conn, tenant_id, owner):
        first = await principal_from_okta(conn, claims())
        second = await principal_from_okta(conn, claims(groups=["gk-admin"], email="Pat.New@Test.example"))
        assert second.user_id == first.user_id
        assert (second.role, second.email, second.scopes) == ("admin", "pat.new@test.example", ("docs:read", "docs:admin"))
    run_in_rollback(test)


def test_every_login_records_when_it_happened():
    async def test(conn, tenant_id, owner):
        p = await principal_from_okta(conn, claims())
        first = await conn.fetchval("SELECT last_login_at FROM users WHERE id = $1", uuid.UUID(p.user_id))
        await principal_from_okta(conn, claims())
        second = await conn.fetchval("SELECT last_login_at FROM users WHERE id = $1", uuid.UUID(p.user_id))
        assert first is not None and second >= first
    run_in_rollback(test)


def test_first_login_adopts_precreated_user_by_email():
    async def test(conn, tenant_id, owner):
        async with tenant_tx(owner, tenant_id):  # pre-created outside this transaction, as SCIM/seed would
            seeded = await owner.fetchval(
                "INSERT INTO users (tenant_id, email, role) VALUES ($1, 'pat@test.example', 'employee') RETURNING id",
                uuid.UUID(tenant_id),
            )
        p = await principal_from_okta(conn, claims())
        assert p.user_id == str(seeded)
        assert await conn.fetchval("SELECT external_id FROM users WHERE id = $1", seeded) == "00uTEST"
    run_in_rollback(test)


def test_cannot_take_over_email_linked_to_another_idp_user():
    async def test(conn, tenant_id, owner):
        await principal_from_okta(conn, claims(sub="00uOWNER"))
        with pytest.raises(AuthBridgeError, match="different IdP user"):
            await principal_from_okta(conn, claims(sub="00uATTACKER"))
    run_in_rollback(test)


def test_deactivated_user_is_refused():
    async def test(conn, tenant_id, owner):
        p = await principal_from_okta(conn, claims())
        await conn.execute("UPDATE users SET active = false WHERE id = $1", uuid.UUID(p.user_id))
        with pytest.raises(AuthBridgeError, match="deactivated"):
            await principal_from_okta(conn, claims())
    run_in_rollback(test)


def test_unknown_issuer_is_refused():
    async def test(conn, tenant_id, owner):
        with pytest.raises(AuthBridgeError, match="no tenant"):
            await principal_from_okta(conn, claims(iss="https://someone-elses-okta.example"))
    run_in_rollback(test)


def test_tenant_comes_from_issuer_not_claims():
    async def test(conn, tenant_id, owner):
        globex = await conn.fetchval("SELECT id FROM tenants WHERE name = 'Globex'")
        # Extra claims a user might control can't move them to another tenant.
        p = await principal_from_okta(conn, claims(tenant_id=str(globex), tenant="Globex"))
        assert p.tenant_id == tenant_id
    run_in_rollback(test)


def test_missing_email_is_refused():
    async def test(conn, tenant_id, owner):
        with pytest.raises(AuthBridgeError, match="no email"):
            await principal_from_okta(conn, claims(email=None))
    run_in_rollback(test)


# ---------- SAML ----------

from app.auth.bridge import principal_from_saml, role_from_app_roles  # noqa: E402

SAML_ISSUER = "https://test-saml-idp.example/globex"


def run_in_saml_rollback(test):
    run_as_app(test, idp_type="entra_saml", issuer=SAML_ISSUER)


@pytest.mark.parametrize("values, role", [
    (["hr_manager"], "hr_manager"),
    (["admin", "offline_access", "default-roles-globex"], "admin"),  # Keycloak extras ignored
    (["admin", "employee"], "employee"),                               # lowest role wins
])
def test_role_from_app_roles(values, role):
    assert role_from_app_roles(values) == role


def test_saml_login_creates_user_from_entra_style_claims():
    async def test(conn, tenant_id, owner):
        p = await principal_from_saml(conn, issuer=SAML_ISSUER, name_id="G-1", attributes={
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress": ["Sam@Globex.example"],
            "http://schemas.microsoft.com/ws/2008/06/identity/claims/role": ["admin"],
        })
        assert (p.tenant_id, p.email, p.role) == (tenant_id, "sam@globex.example", "admin")
    run_in_saml_rollback(test)


def test_saml_cannot_log_into_an_okta_tenant():
    async def test(conn, tenant_id, owner):
        # Same issuer string, but the tenant is configured for Okta OIDC, not SAML.
        await owner.execute("UPDATE tenants SET idp_type = 'okta_oidc' WHERE id = $1::uuid", tenant_id)
        with pytest.raises(AuthBridgeError, match="no tenant"):
            await principal_from_saml(conn, issuer=SAML_ISSUER, name_id="G-1",
                                      attributes={"email": ["x@y.example"], "role": ["admin"]})
    run_in_saml_rollback(test)


def test_saml_without_role_is_refused():
    async def test(conn, tenant_id, owner):
        with pytest.raises(AuthBridgeError, match="no Gatekeeper role"):
            await principal_from_saml(conn, issuer=SAML_ISSUER, name_id="G-1",
                                      attributes={"email": ["x@y.example"], "role": ["offline_access"]})
    run_in_saml_rollback(test)
