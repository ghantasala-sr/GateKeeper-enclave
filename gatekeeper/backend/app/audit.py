"""Write-only audit log. See migrations/008_audit.sql.

The app role can INSERT but never read, update, or delete. Entries are written
under the tenant's RLS context, so a request can only log for its own tenant.
"""

import asyncpg

from app import db


async def record(conn: asyncpg.Connection, *, tenant_id: str, action: str, user_id: str | None = None,
                 question: str | None = None, chunk_ids: list[str] | None = None, detail: str | None = None) -> None:
    """Write one entry. `conn` must be in a transaction scoped to `tenant_id` (db.run_as...)."""
    # No RETURNING: that would need SELECT, which the app deliberately doesn't have.
    await conn.execute(
        "INSERT INTO audit_log (tenant_id, user_id, action, question, chunk_ids, detail) "
        "VALUES ($1::uuid, $2::uuid, $3, $4, $5::uuid[], $6)",
        tenant_id, user_id, action, question, chunk_ids, detail,
    )


async def record_as_tenant(tenant_id: str, **entry) -> None:
    """Write one entry in its own tenant-scoped transaction."""
    async with db.run_as_tenant(tenant_id) as conn:
        await record(conn, tenant_id=tenant_id, **entry)
