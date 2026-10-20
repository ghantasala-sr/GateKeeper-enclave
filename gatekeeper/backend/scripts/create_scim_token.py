"""Create (or rotate) a tenant's SCIM bearer token. Prints it ONCE; only its hash is stored.

Usage: python -m scripts.create_scim_token "Acme Corp"
"""

import asyncio
import hashlib
import secrets
import sys

import asyncpg

from app.config import settings


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create(tenant_name: str) -> str:
    token = "gk_scim_" + secrets.token_urlsafe(32)
    conn = await asyncpg.connect(settings.migration_database_url)
    try:
        updated = await conn.execute("UPDATE tenants SET scim_token_hash = $1 WHERE name = $2",
                                     hash_token(token), tenant_name)
    finally:
        await conn.close()
    if updated != "UPDATE 1":
        raise SystemExit(f"no tenant named {tenant_name!r}")
    return token


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(asyncio.run(create(sys.argv[1])))
