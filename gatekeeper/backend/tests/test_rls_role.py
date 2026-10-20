"""Row-level security role rules inside each tenant.

Runs as the real app role (gatekeeper_app). Needs Postgres migrated, seeded, and ingested.
"""

import asyncio

import asyncpg
import pytest

from app import db
from app.auth.bridge import Principal
from app.config import settings
from app.rag.search import search

ALLOWED = {
    "employee": {"public"},
    "hr_manager": {"public", "hr_confidential"},
    "admin": {"public", "hr_confidential", "executive"},
}
# The seed has 4 public, 3 hr_confidential, 3 executive documents per tenant.
DOC_COUNTS = {"public": 4, "hr_confidential": 3, "executive": 3}


def run(test):
    async def main():
        pool = await asyncpg.create_pool(settings.app_database_url, min_size=1, max_size=2)
        db.pool = pool
        try:
            async with pool.acquire() as conn:
                tenants = {r["name"]: str(r["id"]) for r in await conn.fetch("SELECT id, name FROM tenants")}
            return await test(tenants)
        finally:
            db.pool = None
            await pool.close()
    return asyncio.run(main())


async def visible(conn, table: str) -> dict[str, set]:
    rows = await conn.fetch(f"SELECT DISTINCT tenant_id, classification FROM {table}")
    return {"tenants": {str(r["tenant_id"]) for r in rows}, "classifications": {r["classification"] for r in rows}}


@pytest.mark.parametrize("tenant", ["Acme Corp", "Globex"])
@pytest.mark.parametrize("role", list(ALLOWED))
def test_role_sees_exactly_its_classifications(tenant, role):
    async def test(tenants):
        async with db.run_as_tenant(tenants[tenant], role) as conn:
            for table in ("documents", "chunks"):
                v = await visible(conn, table)
                assert v["classifications"] == ALLOWED[role], table
                assert v["tenants"] == {tenants[tenant]}, table
            n_docs = await conn.fetchval("SELECT count(*) FROM documents")
            assert n_docs == sum(DOC_COUNTS[c] for c in ALLOWED[role])
    run(test)


@pytest.mark.parametrize("role", ["", "superadmin", "ADMIN", "admin ", "employee' OR '1'='1"])
def test_unknown_or_missing_role_sees_nothing(role):
    async def test(tenants):
        async with db.run_as_tenant(tenants["Acme Corp"], role) as conn:
            assert await conn.fetchval("SELECT count(*) FROM documents") == 0
            assert await conn.fetchval("SELECT count(*) FROM chunks") == 0
    run(test)


def test_role_without_tenant_sees_nothing():
    async def test(tenants):
        async with db.get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.role', 'admin', true)")
                assert await conn.fetchval("SELECT count(*) FROM chunks") == 0
    run(test)


def test_role_does_not_linger_on_a_reused_connection():
    async def test(tenants):
        async with db.get_pool().acquire() as conn:
            async with conn.transaction():
                await db.set_tenant_context(conn, tenants["Acme Corp"], "admin")
                assert await conn.fetchval("SELECT count(*) FROM documents") == 10
            async with conn.transaction():
                await db.set_tenant_context(conn, tenants["Acme Corp"])  # tenant only, no role
                assert await conn.fetchval("SELECT count(*) FROM documents") == 0
    run(test)


@pytest.mark.parametrize("role, question", [
    ("employee", "What are the senior engineer salary bands?"),
    ("employee", "What is the Q3 revenue target and the acquisition price?"),
    ("hr_manager", "What is the Q3 revenue target and the acquisition price?"),
])
def test_search_only_returns_allowed_chunks(role, question):
    async def test(tenants):
        p = Principal("u", tenants["Acme Corp"], "x@acme.example", role, ("docs:read",))
        async with db.run_as(p) as conn:
            chunks = await search(conn, question)
        assert chunks, "search should still find something the role may see"
        assert {c.classification for c in chunks} <= ALLOWED[role]
        assert {c.tenant for c in chunks} == {"Acme Corp"}
    run(test)
