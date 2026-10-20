-- The running app connects as this limited role, never as the table owner.
-- Postgres skips row-level security for superusers and (by default) table
-- owners, so the RLS policies in later migrations only bind a role like this.
-- The password is set by scripts/migrate.py from APP_DB_PASSWORD.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'gatekeeper_app') THEN
        CREATE ROLE gatekeeper_app LOGIN;  -- new roles have none of the powers below
    END IF;
    -- Verify rather than ALTER: only a superuser may even *clear* SUPERUSER or
    -- BYPASSRLS, and managed Postgres (Cloud SQL) has no superuser. A role left
    -- with any of these by hand would silently defeat row-level security.
    IF EXISTS (
        SELECT FROM pg_roles WHERE rolname = 'gatekeeper_app'
          AND (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication)
    ) THEN
        RAISE EXCEPTION 'gatekeeper_app must not have SUPERUSER, BYPASSRLS, CREATEDB, CREATEROLE or REPLICATION';
    END IF;
END
$$;

-- No creating objects anywhere in the schema.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO gatekeeper_app;

-- Read-only documents; users can be created/updated at login and by SCIM.
GRANT SELECT ON tenants, documents, chunks TO gatekeeper_app;
GRANT SELECT, INSERT, UPDATE ON users TO gatekeeper_app;
