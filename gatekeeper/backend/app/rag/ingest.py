"""Split seed documents into chunks, embed them, and store them in `chunks`.

Re-running replaces all chunks. Runs as the database owner (MIGRATION_DATABASE_URL),
which need not be a superuser: each tenant is processed inside its own RLS context.
Run `python -m scripts.seed` first. Usage: python -m app.rag.ingest
"""

import asyncio
import re
from pathlib import Path

import asyncpg

from app.config import settings
from app.db import set_tenant_context
from app.rag.embeddings import embed, to_pgvector
from scripts.seed import SEED_DIR, TENANTS, parse_front_matter


def split_sections(title: str, body: str) -> list[str]:
    """One chunk per '## ' section, each prefixed with the document title for context."""
    sections = re.split(r"\n(?=## )", body)
    chunks = []
    for section in sections:
        section = section.strip()
        # Skip the bare '# Title' line and any section with no real content.
        if section.startswith("# ") and "\n" not in section:
            continue
        chunks.append(f"{title}\n\n{section}")
    return chunks


async def main() -> None:
    conn = await asyncpg.connect(settings.migration_database_url)
    try:
        async with conn.transaction():
            for folder, (tenant_name, *_) in TENANTS.items():
                tenant_id = await conn.fetchval("SELECT id FROM tenants WHERE name = $1", tenant_name)
                if tenant_id is None:
                    raise SystemExit(f"tenant {tenant_name!r} not found; run scripts.seed first")
                await set_tenant_context(conn, str(tenant_id), "admin")
                # Scope explicitly: a superuser owner ignores RLS, and an unscoped
                # DELETE would wipe the tenants already ingested in this run.
                await conn.execute("DELETE FROM chunks WHERE tenant_id = $1", tenant_id)
                total = 0
                for path in sorted((SEED_DIR / folder).glob("*.md")):
                    meta, body = parse_front_matter(path.read_text())
                    doc = await conn.fetchrow(
                        """
                        SELECT d.id, d.tenant_id, d.classification
                        FROM documents d JOIN tenants t ON t.id = d.tenant_id
                        WHERE t.name = $1 AND d.reference = $2
                        """,
                        tenant_name, meta["reference"],
                    )
                    if doc is None:
                        raise SystemExit(f"{meta['reference']} not in documents; run scripts.seed first")

                    texts = split_sections(meta["title"], body)
                    vectors = await asyncio.to_thread(embed, texts)
                    await conn.executemany(
                        """
                        INSERT INTO chunks (document_id, tenant_id, classification, content, embedding)
                        VALUES ($1, $2, $3, $4, $5::vector)
                        """,
                        [
                            (doc["id"], doc["tenant_id"], doc["classification"], text, to_pgvector(vec))
                            for text, vec in zip(texts, vectors)
                        ],
                    )
                    total += len(texts)
                print(f"{tenant_name}: {total} chunks")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
