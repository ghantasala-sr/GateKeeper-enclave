"""Postgres row-level security keeps each company's rows separate.

Runs as the real app role (gatekeeper_app). Needs Postgres migrated, seeded, and ingested.
Uses the admin role wherever full counts are expected; role rules are tested in test_rls_role.py.
"""

import asyncio
import uuid

import asyncpg
import pytest

from app import db
from app.auth.bridge import Principal
from app.config import settings
from app.rag.search import search
from tests.owner import connect as owner_connect
from tests.owner import tenant_tx

TABLES = ("documents", "chunks", "users")


def run(test):
    """Run test(pool, tenants) with a 1-connection app pool, so every acquire reuses it."""
    async def main():
        pool = await asyncpg.create_pool(settings.app_database_url, min_size=1, max_size=1)
        db.pool = pool
        try:
            async with pool.acquire() as conn:
                tenants = {r["name"]: str(r["id"]) for r in await conn.fetch("SELECT id, name FROM tenants")}
            return await test(pool, tenants)
        finally:
            db.pool = None
            await pool.close()
    return asyncio.run(main())


async def counts(conn) -> dict:
    return {t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in TABLES}


async def tenant_ids_visible(conn, table: str) -> set[str]:
    return {str(r["tenant_id"]) for r in await conn.fetch(f"SELECT DISTINCT tenant_id FROM {table}")}


# ---------- fails closed ----------

def test_no_tenant_set_sees_nothing():
    async def test(pool, tenants):
        async with pool.acquire() as conn:
            assert await counts(conn) == {"documents": 0, "chunks": 0, "users": 0}
    run(test)


def test_after_a_scoped_request_the_next_unscoped_query_sees_nothing():
    # Same physical connection: first a request scoped to Acme, then a query with
    # no tenant. The setting must not linger, and '' must not crash the ::uuid cast.
    async def test(pool, tenants):
        async with db.run_as_tenant(tenants["Acme Corp"], "admin") as conn:
            assert (await counts(conn))["chunks"] > 0
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT current_setting('app.tenant_id', true)") == ""
            assert await counts(conn) == {"documents": 0, "chunks": 0, "users": 0}
    run(test)


def test_tenant_context_requires_a_transaction():
    async def test(pool, tenants):
        async with pool.acquire() as conn:
            with pytest.raises(RuntimeError, match="inside a transaction"):
                await db.set_tenant_context(conn, tenants["Acme Corp"])
    run(test)


# ---------- each tenant sees only itself ----------

@pytest.mark.parametrize("tenant", ["Acme Corp", "Globex"])
def test_each_tenant_sees_only_its_own_rows(tenant):
    async def test(pool, tenants):
        # Per-tenant counts as the owner, inside that tenant's context (on Cloud SQL
        # nothing bypasses RLS). Documents and chunks are fixed by the seed data.
        owner = await owner_connect()
        async with tenant_tx(owner, tenants[tenant]):
            expected = {t: await owner.fetchval(f"SELECT count(*) FROM {t} WHERE tenant_id = $1::uuid", tenants[tenant])
                        for t in TABLES}
        await owner.close()
        assert expected["documents"] == 10 and expected["chunks"] == 56 and expected["users"] >= 3
        async with db.run_as_tenant(tenants[tenant], "admin") as conn:
            assert await counts(conn) == expected
            for table in TABLES:
                assert await tenant_ids_visible(conn, table) == {tenants[tenant]}
    run(test)


def test_asking_for_another_tenant_by_id_returns_nothing():
    async def test(pool, tenants):
        async with db.run_as_tenant(tenants["Acme Corp"], "admin") as conn:
            for table in TABLES:
                n = await conn.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id = $1::uuid", tenants["Globex"])
                assert n == 0, table
            assert await conn.fetchval(
                "SELECT count(*) FROM documents WHERE reference LIKE 'GLOBEX-%'") == 0
    run(test)


def test_unknown_tenant_sees_nothing():
    async def test(pool, tenants):
        async with db.run_as_tenant(str(uuid.uuid4()), "admin") as conn:
            assert await counts(conn) == {"documents": 0, "chunks": 0, "users": 0}
    run(test)


# ---------- writes can't cross tenants either ----------

def test_cannot_create_user_in_another_tenant():
    async def test(pool, tenants):
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
            async with db.run_as_tenant(tenants["Acme Corp"]) as conn:
                await conn.execute(
                    "INSERT INTO users (tenant_id, email, role) VALUES ($1::uuid, 'spy@globex.example', 'admin')",
                    tenants["Globex"])
    run(test)


def test_cannot_move_a_user_to_another_tenant():
    async def test(pool, tenants):
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
            async with db.run_as_tenant(tenants["Acme Corp"]) as conn:
                await conn.execute("UPDATE users SET tenant_id = $1::uuid WHERE email = 'employee@acme.example'",
                                   tenants["Globex"])
    run(test)


def test_cannot_update_another_tenants_user():
    async def test(pool, tenants):
        async with db.run_as_tenant(tenants["Acme Corp"]) as conn:
            result = await conn.execute("UPDATE users SET role = 'employee' WHERE email = 'admin@globex.example'")
            assert result == "UPDATE 0"  # invisible, so untouched
    run(test)


# ---------- the cross-tenant salary leak, closed ----------

@pytest.mark.parametrize("tenant, other_prefix", [("Acme Corp", "GLOBEX-"), ("Globex", "ACME-")])
def test_salary_search_returns_only_own_tenant(tenant, other_prefix):
    async def test(pool, tenants):
        p = Principal("u", tenants[tenant], "x@example", "admin", ("docs:read",))
        async with db.run_as(p) as conn:
            chunks = await search(conn, "What are the senior engineer salary bands?")
        assert len(chunks) == 5
        assert {c.tenant for c in chunks} == {tenant}
        assert not any(c.reference.startswith(other_prefix) for c in chunks)
    run(test)
