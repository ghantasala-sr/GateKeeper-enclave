-- Which IdP each tenant logs in through. A verified login maps to a tenant
-- ONLY by the issuer that signed it, never by anything the user supplies.
ALTER TABLE tenants ADD COLUMN idp_issuer TEXT UNIQUE;

-- The user's ID inside their IdP (Okta `sub`). Stable even if their email changes.
ALTER TABLE users ADD CONSTRAINT users_tenant_external_id_key UNIQUE (tenant_id, external_id);
