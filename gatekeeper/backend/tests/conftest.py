import asyncio

import asyncpg
import pytest

from app.config import settings


def _owner(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(settings.migration_database_url)
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()
    return asyncio.run(run())


@pytest.fixture(scope="session", autouse=True)
def keep_audit_log_clean():
    """Tests hit real routes, which write audit rows. Remove the ones this run wrote.

    (Only the owner can delete; the app role is insert-only. Don't run the suite
    against a server whose audit log you need to keep.)
    """
    start = _owner("SELECT coalesce(max(id), 0) FROM audit_log")
    yield
    _owner("DELETE FROM audit_log WHERE id > $1", start)
