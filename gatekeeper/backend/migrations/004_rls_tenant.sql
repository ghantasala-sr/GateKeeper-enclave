-- Company separation enforced by Postgres itself.
--
-- Each request sets app.tenant_id for its transaction (app/db.py). Every query
-- on these tables then gets an invisible "WHERE tenant_id = <that tenant>".
--
-- NULLIF matters: once a connection has used set_config(..., true), the setting
-- reads back as '' (not NULL) in later transactions, and ''::uuid would raise an
-- error. With NULLIF, "no tenant set" means NULL, which matches no rows: it
-- fails closed instead of crashing.
--
-- FORCE makes the table owner obey the policy too. (Superusers still bypass all
-- RLS, which is why the app must never connect as one; see 003_app_role.sql.)

ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chunks
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON documents
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- WITH CHECK also stops a write from creating or moving a user into another tenant.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON users
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- `tenants` has no RLS: login must look a tenant up by IdP issuer before any
-- tenant is known. The app role can only read it.
