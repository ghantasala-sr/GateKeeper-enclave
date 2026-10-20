CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE tenants (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name      TEXT NOT NULL UNIQUE,
    idp_type  TEXT NOT NULL CHECK (idp_type IN ('okta_oidc', 'entra_saml'))
);

CREATE TABLE users (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    email        TEXT NOT NULL,
    external_id  TEXT,              -- the user's ID inside Okta/Microsoft
    role         TEXT NOT NULL CHECK (role IN ('employee', 'hr_manager', 'admin')),
    active       BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (tenant_id, email)
);

CREATE TABLE documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    reference       TEXT NOT NULL,  -- e.g. ACME-HR-01, from the seed file
    title           TEXT NOT NULL,
    classification  TEXT NOT NULL CHECK (classification IN ('public', 'hr_confidential', 'executive')),
    UNIQUE (tenant_id, reference),
    -- Target for the chunks foreign key below.
    UNIQUE (id, tenant_id, classification)
);

CREATE TABLE chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL,
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    classification  TEXT NOT NULL,
    content         TEXT NOT NULL,
    embedding       vector(384),    -- size matches all-MiniLM-L6-v2
    -- A chunk's tenant and classification must always match its document's,
    -- because RLS filters chunks on these copied columns.
    FOREIGN KEY (document_id, tenant_id, classification)
        REFERENCES documents (id, tenant_id, classification)
        ON UPDATE CASCADE ON DELETE CASCADE
);
