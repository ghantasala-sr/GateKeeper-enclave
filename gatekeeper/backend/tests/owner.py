"""Owner-level test setup and teardown that never relies on bypassing RLS.

On Cloud SQL the database owner is not a superuser, so the FORCE'd row-level
security on users, documents and chunks applies to it like anyone else. These
helpers set the tenant context the same way scripts/seed.py does.
"""

import asyncio
from contextlib import asynccontextmanager

import asyncpg

from app.config import settings
from app.db import set_tenant_context


async def connect() -> asyncpg.Connection:
    return await asyncpg.connect(settings.migration_database_url)


@asynccontextmanager
async def tenant_tx(conn: asyncpg.Connection, tenant_id):
    """A transaction on an owner connection, scoped to one tenant (as admin)."""
    async with conn.transaction():
        await set_tenant_context(conn, str(tenant_id), "admin")
        yield conn


def owner(sql: str, *args, tenant=None, fetch=False):
    """Run SQL on RLS tables as the owner.

    With `tenant`, runs once in that tenant's context. Without it, runs once per
    tenant and concatenates the results, so "delete these users" or "find this
    email" behave like a cross-tenant query would for a superuser.
    """
    async def run():
        conn = await connect()
        try:
            # A superuser (or BYPASSRLS) owner ignores the tenant context, so one pass
            # already sees every tenant; looping would return each row once per tenant.
            bypasses_rls = await conn.fetchval(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
            if tenant:
                tenant_ids = [tenant]
            elif bypasses_rls:
                tenant_ids = [None]
            else:
                tenant_ids = [r["id"] for r in await conn.fetch("SELECT id FROM tenants")]
            results = []
            for tid in tenant_ids:
                async with (tenant_tx(conn, tid) if tid else conn.transaction()):
                    if fetch:
                        results.extend(await conn.fetch(sql, *args))
                    else:
                        results.append(await conn.execute(sql, *args))
            return results
        finally:
            await conn.close()
    return asyncio.run(run())


def owner_plain(sql: str, *args, fetch=False):
    """Run SQL as the owner with no tenant context: for `tenants` (no RLS) and `audit_log` (owner-readable)."""
    async def run():
        conn = await connect()
        try:
            return await (conn.fetch if fetch else conn.execute)(sql, *args)
        finally:
            await conn.close()
    return asyncio.run(run())
