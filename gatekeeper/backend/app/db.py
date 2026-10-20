from contextlib import asynccontextmanager

import asyncpg

from app.config import settings

pool: asyncpg.Pool | None = None


async def open_pool() -> None:
    global pool
    # The limited gatekeeper_app role, never the owner (see migrations/003_app_role.sql).
    pool = await asyncpg.create_pool(settings.app_database_url, min_size=1, max_size=10)


async def close_pool() -> None:
    if pool is not None:
        await pool.close()


def get_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("database pool is not open")
    return pool


async def set_tenant_context(conn: asyncpg.Connection, tenant_id: str, role: str = "") -> None:
    """Tell RLS which tenant (and role) this transaction belongs to.

    set_config(..., true) is the same as SET LOCAL: it ends with the transaction.
    Never use plain SET here. Pooled connections are reused, and a plain SET
    would carry one user's tenant into the next user's request.
    """
    if not conn.is_in_transaction():
        raise RuntimeError("tenant context must be set inside a transaction")
    await conn.execute(
        "SELECT set_config('app.tenant_id', $1, true), set_config('app.role', $2, true)",
        tenant_id, role,
    )


@asynccontextmanager
async def run_as_tenant(tenant_id: str, role: str = ""):
    """A pooled connection inside a transaction that RLS scopes to one tenant."""
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await set_tenant_context(conn, tenant_id, role)
            yield conn


def run_as(principal):
    """Scope a transaction to a verified Principal's tenant and role."""
    return run_as_tenant(principal.tenant_id, principal.role)
