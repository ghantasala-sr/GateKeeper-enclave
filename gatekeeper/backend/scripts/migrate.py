"""Apply backend/migrations/*.sql in filename order, each one exactly once.

Runs as the database owner (MIGRATION_DATABASE_URL). Usage: python -m scripts.migrate
"""

import asyncio
from pathlib import Path

import asyncpg

from app.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


async def main() -> None:
    conn = await asyncpg.connect(settings.migration_database_url)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename    TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied = {r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (filename) VALUES ($1)", path.name)
            print(f"applied {path.name}")

        # Keep the app role's password in sync with .env (never stored in a migration).
        if settings.app_db_password and await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'gatekeeper_app'"):
            sql = await conn.fetchval("SELECT format('ALTER ROLE gatekeeper_app PASSWORD %L', $1::text)", settings.app_db_password)
            await conn.execute(sql)
        print("migrations up to date")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
