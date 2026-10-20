"""Filtered vector search still returns enough results.

Simulates a large table: forces the HNSW index and a small candidate list
(ef_search), which is what makes RLS filtering leave too few rows at scale.
"""

import asyncio

import asyncpg
import pytest

from app import db
from app.auth.bridge import Principal
from app.config import settings
from app.rag.embeddings import embed, to_pgvector
from app.rag.search import search

QUESTION = "What are the senior engineer salary bands?"  # nearest chunks are all HR


def as_acme(role: str, test):
    async def main():
        db.pool = await asyncpg.create_pool(settings.app_database_url, min_size=1, max_size=1)
        try:
            async with db.get_pool().acquire() as conn:
                tenant_id = str(await conn.fetchval("SELECT id FROM tenants WHERE name = 'Acme Corp'"))
            async with db.run_as(Principal("u", tenant_id, "x@acme.example", role, ("docs:read",))) as conn:
                await conn.execute("SET LOCAL enable_seqscan = off")  # use the index, as at scale
                await conn.execute("SET LOCAL hnsw.ef_search = 10")    # few candidates, as at scale
                return await test(conn)
        finally:
            await db.pool.close()
            db.pool = None
    return asyncio.run(main())


def test_without_iterative_scan_filtering_starves_results():
    # The problem iterative scans solve, reproduced.
    async def test(conn):
        q = to_pgvector(embed([QUESTION])[0])
        rows = await conn.fetch("SELECT id FROM chunks ORDER BY embedding <=> $1::vector LIMIT 5", q)
        plan = "\n".join(r[0] for r in await conn.fetch(
            "EXPLAIN SELECT id FROM chunks ORDER BY embedding <=> $1::vector LIMIT 5", q))
        assert "chunks_embedding_hnsw" in plan
        assert len(rows) < 5
    as_acme("employee", test)


@pytest.mark.parametrize("role", ["employee", "hr_manager", "admin"])
def test_search_returns_full_results_for_every_role(role):
    async def test(conn):
        chunks = await search(conn, QUESTION)
        assert len(chunks) == 5
        distances = [c.distance for c in chunks]
        assert distances == sorted(distances)  # re-sorted after relaxed_order
        return chunks
    chunks = as_acme(role, test)
    allowed = {"employee": {"public"}, "hr_manager": {"public", "hr_confidential"},
               "admin": {"public", "hr_confidential", "executive"}}[role]
    assert {c.classification for c in chunks} <= allowed


def test_iterative_scan_does_not_leak_into_the_next_transaction():
    async def main():
        conn = await asyncpg.connect(settings.app_database_url)
        try:
            await conn.execute("SELECT '[1]'::vector")  # load pgvector so its settings are real, not placeholders
            async with conn.transaction():
                await conn.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
            return await conn.fetchval("SHOW hnsw.iterative_scan")
        finally:
            await conn.close()
    assert asyncio.run(main()) == "off"
