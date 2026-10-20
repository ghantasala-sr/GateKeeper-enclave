-- SCIM provisioning.

-- Each tenant's IdP gets its own SCIM bearer token. Only a SHA-256 hash is
-- stored; the token itself is shown once by scripts/create_scim_token.py.
-- The token identifies the tenant, so Acme's Okta can only ever touch Acme.
ALTER TABLE tenants ADD COLUMN scim_token_hash TEXT UNIQUE;

-- Fields SCIM sends and expects back.
ALTER TABLE users
    ADD COLUMN given_name   TEXT,
    ADD COLUMN family_name  TEXT,
    ADD COLUMN created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN updated_at   TIMESTAMPTZ NOT NULL DEFAULT now();
