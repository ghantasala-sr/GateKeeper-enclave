import asyncio
from dataclasses import dataclass

import asyncpg

from app.rag.embeddings import embed, to_pgvector


@dataclass(frozen=True)
class Chunk:
    id: str
    tenant: str
    reference: str
    classification: str
    content: str
    distance: float


async def search(conn: asyncpg.Connection, query: str, k: int = 5) -> list[Chunk]:
    """Top-k chunks the current user may see. `conn` must come from db.run_as().

    There is deliberately no tenant or role filter here: RLS on `chunks` and
    `documents` removes every row this user isn't allowed to see.
    """
    [vector] = await asyncio.to_thread(embed, [query])
    # The HNSW index finds nearest chunks across ALL tenants and roles, then RLS
    # filters them, which can leave fewer than k. Iterative scans keep searching
    # until k allowed rows are found. relaxed_order may return them slightly out
    # of order, so the outer query re-sorts by exact distance.
    await conn.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
    rows = await conn.fetch(
        """
        WITH nearest AS MATERIALIZED (
            SELECT c.id, c.tenant_id, c.document_id, c.classification, c.content,
                   c.embedding <=> $1::vector AS distance
            FROM chunks c
            ORDER BY c.embedding <=> $1::vector
            LIMIT $2
        )
        SELECT n.id, t.name AS tenant, d.reference, n.classification, n.content, n.distance
        FROM nearest n
        JOIN documents d ON d.id = n.document_id
        JOIN tenants t ON t.id = n.tenant_id
        ORDER BY n.distance
        """,
        to_pgvector(vector), k,
    )
    return [
        Chunk(str(r["id"]), r["tenant"], r["reference"], r["classification"], r["content"], r["distance"])
        for r in rows
    ]
