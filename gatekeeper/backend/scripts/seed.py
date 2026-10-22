"""Load the fake tenants, users, and documents from backend/seed/.

Safe to re-run. Runs as the database owner (MIGRATION_DATABASE_URL). The owner
need not be a superuser (Cloud SQL has none): row-level security applies to it
too, so each tenant's rows are written inside that tenant's context.
Usage: python -m scripts.seed
"""

import asyncio
from pathlib import Path

from app import db
from app.config import settings
from app.db import set_tenant_context

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"

# Folder name -> (tenant name, IdP type, email domain, IdP issuer)
TENANTS = {
    "acme": ("Acme Corp", "okta_oidc", "acme.example", settings.okta_issuer.rstrip("/") or None),
    # Keycloak stands in for Microsoft Entra ID locally; both speak SAML.
    "globex": ("Globex", "entra_saml", "globex.example", settings.saml_idp_entity_id or None),
}

ROLES = ("employee", "hr_manager", "admin")


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split a '---' delimited header of `key: value` lines from the body."""
    _, header, body = text.split("---\n", 2)
    meta = {}
    for line in header.strip().splitlines():
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, body.strip()


async def main() -> None:
    conn = await db.connect_as_owner()
    try:
        async with conn.transaction():
            for folder, (name, idp_type, domain, issuer) in TENANTS.items():
                tenant_id = await conn.fetchval(
                    """
                    INSERT INTO tenants (name, idp_type, idp_issuer) VALUES ($1, $2, $3)
                    ON CONFLICT (name) DO UPDATE SET
                        idp_type = EXCLUDED.idp_type,
                        idp_issuer = COALESCE(EXCLUDED.idp_issuer, tenants.idp_issuer)
                    RETURNING id
                    """,
                    name, idp_type, issuer,
                )
                # `tenants` has no RLS; everything below does. "admin" so the
                # role policy also allows writing hr_confidential/executive docs.
                await set_tenant_context(conn, str(tenant_id), "admin")

                # Seed users. Each IdP login (or SCIM) links to these rows by email.
                for role in ROLES:
                    await conn.execute(
                        """
                        INSERT INTO users (tenant_id, email, role) VALUES ($1, $2, $3)
                        ON CONFLICT (tenant_id, email) DO UPDATE SET role = EXCLUDED.role
                        """,
                        tenant_id, f"{role.replace('_', '.')}@{domain}", role,
                    )

                files = sorted((SEED_DIR / folder).glob("*.md"))
                for path in files:
                    meta, _ = parse_front_matter(path.read_text())
                    await conn.execute(
                        """
                        INSERT INTO documents (tenant_id, reference, title, classification)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (tenant_id, reference)
                        DO UPDATE SET title = EXCLUDED.title, classification = EXCLUDED.classification
                        """,
                        tenant_id, meta["reference"], meta["title"], meta["classification"],
                    )
                print(f"{name}: {len(ROLES)} users, {len(files)} documents")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
