"""The full access matrix, for every tenant x role x classification.

Each case is checked at three layers, all as the real app role (gatekeeper_app):
  1. the documents table   2. the chunks table   3. the agent's search tool.
No Claude calls. Needs Postgres migrated, seeded, and ingested.
"""

import asyncio
import itertools

import asyncpg
import pytest

from app import db
from app.auth.bridge import ROLE_SCOPES, Principal
from app.config import settings
from app.rag.agent import search_documents

EXPECTED = {
    ("employee",   "public"):          True,
    ("employee",   "hr_confidential"): False,
    ("employee",   "executive"):       False,
    ("hr_manager", "public"):          True,
    ("hr_manager", "hr_confidential"): True,
    ("hr_manager", "executive"):       False,
    ("admin",      "public"):          True,
    ("admin",      "hr_confidential"): True,
    ("admin",      "executive"):       True,
}

TENANTS = ("Acme Corp", "Globex")
ROLES = ("employee", "hr_manager", "admin")
CLASSIFICATIONS = ("public", "hr_confidential", "executive")

# A question whose best matches are documents of that classification.
PROBE = {
    "public": "How many PTO days do employees get?",
    "hr_confidential": "What are the senior engineer salary bands?",
    "executive": "What is the Q3 revenue target in the financial plan?",
}


def run(test):
    async def main():
        db.pool = await asyncpg.create_pool(settings.app_database_url, min_size=1, max_size=2)
        try:
            async with db.get_pool().acquire() as conn:
                tenants = {r["name"]: str(r["id"]) for r in await conn.fetch("SELECT id, name FROM tenants")}
            return await test(tenants)
        finally:
            await db.pool.close()
            db.pool = None
    return asyncio.run(main())


def as_user(tenants, tenant, role) -> Principal:
    return Principal("00000000-0000-0000-0000-000000000000", tenants[tenant], "matrix@example", role, ROLE_SCOPES[role])


@pytest.mark.parametrize("tenant, role, classification", list(itertools.product(TENANTS, ROLES, CLASSIFICATIONS)))
def test_matrix(tenant, role, classification):
    allowed = EXPECTED[(role, classification)]

    async def test(tenants):
        p = as_user(tenants, tenant, role)
        async with db.run_as(p) as conn:
            docs = await conn.fetchval("SELECT count(*) FROM documents WHERE classification = $1", classification)
            chunks = await conn.fetchval("SELECT count(*) FROM chunks WHERE classification = $1", classification)
        found, error = await search_documents(p, {"query": PROBE[classification]})
        assert error is None

        # 1-2. The tables: all of that classification, or none of it.
        expected_docs = {"public": 4, "hr_confidential": 3, "executive": 3}[classification] if allowed else 0
        assert docs == expected_docs, "documents"
        assert (chunks > 0) is allowed, "chunks"

        # 3. The agent's tool, asked a question aimed squarely at this classification.
        found_classes = {c.classification for c in found}
        assert (classification in found_classes) is allowed, f"search returned {found_classes}"
        assert all(EXPECTED[(role, c)] for c in found_classes), "search returned a forbidden classification"
        assert {c.tenant for c in found} == {tenant}

    run(test)


@pytest.mark.parametrize("tenant, role", list(itertools.product(TENANTS, ROLES)))
def test_other_tenant_is_never_visible(tenant, role):
    other = next(t for t in TENANTS if t != tenant)

    async def test(tenants):
        p = as_user(tenants, tenant, role)
        async with db.run_as(p) as conn:
            for table in ("documents", "chunks", "users"):
                n = await conn.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id = $1::uuid", tenants[other])
                assert n == 0, table
        for question in PROBE.values():
            found, _ = await search_documents(p, {"query": f"{other} {question}"})  # even naming the other tenant
            assert found and {c.tenant for c in found} == {tenant}

    run(test)
