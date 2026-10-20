"""The running app connects as a limited role that cannot change the schema.

Needs Postgres running with migrations applied.
"""

import asyncio

import asyncpg
import pytest

from app import db
from app.config import settings


def as_app(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(settings.app_database_url)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()
    return asyncio.run(run())


def test_app_pool_connects_as_limited_role():
    async def run():
        await db.open_pool()
        try:
            async with db.get_pool().acquire() as conn:
                return await conn.fetchrow(
                    "SELECT current_user AS who, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        finally:
            await db.close_pool()
    row = asyncio.run(run())
    assert (row["who"], row["rolsuper"], row["rolbypassrls"]) == ("gatekeeper_app", False, False)


def test_app_role_owns_nothing():
    rows = as_app("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user")
    assert rows == []


def test_app_role_can_read_what_it_needs():
    async def run():
        conn = await asyncpg.connect(settings.app_database_url)
        try:
            async with conn.transaction():
                tenant_id = await conn.fetchval("SELECT id FROM tenants WHERE name = 'Acme Corp'")
                await db.set_tenant_context(conn, str(tenant_id), "admin")  # RLS needs a tenant and role
                return [await conn.fetchval(f"SELECT count(*) FROM {t}") for t in ("documents", "chunks", "users")]
        finally:
            await conn.close()
    assert all(n > 0 for n in asyncio.run(run()))


@pytest.mark.parametrize("sql", [
    "CREATE TABLE sneaky (id int)",
    "DROP TABLE chunks",
    "ALTER TABLE chunks DISABLE ROW LEVEL SECURITY",
    "ALTER TABLE documents ADD COLUMN x int",
    "TRUNCATE users",
    "DELETE FROM users",
    "DELETE FROM chunks",
    "UPDATE documents SET classification = 'public'",
    "UPDATE chunks SET classification = 'public'",
    "INSERT INTO tenants (name, idp_type) VALUES ('Evil', 'okta_oidc')",
    "CREATE ROLE sneaky",
    "SET ROLE gatekeeper_owner",
])
def test_app_role_cannot(sql):
    with pytest.raises((asyncpg.InsufficientPrivilegeError, asyncpg.exceptions.InvalidSchemaNameError)):
        as_app(sql)
