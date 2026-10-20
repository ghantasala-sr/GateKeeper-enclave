-- Audit log: who did what, and which data they received.
--
-- The app can only INSERT. It can't read, change, or delete entries, so even a
-- compromised app can't cover its tracks. Reading is for the owner/operators.
--
-- user_id is optional so refused logins are recorded too,
-- e.g. "someone keeps trying to log in to Acme with no Gatekeeper group".
-- `detail` says why or how.
CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id   UUID NOT NULL,
    user_id     UUID,           -- NULL when no user was resolved (e.g. refused login)
    action      TEXT NOT NULL,  -- login, login_refused, chat, request_denied, scim_create, scim_update, scim_deactivate
    question    TEXT,
    chunk_ids   UUID[],
    detail      TEXT
);
CREATE INDEX audit_log_tenant_at ON audit_log (tenant_id, at);

GRANT INSERT ON audit_log TO gatekeeper_app;   -- insert only: no SELECT, UPDATE, DELETE
GRANT USAGE ON SEQUENCE audit_log_id_seq TO gatekeeper_app;

-- A request can only write entries for its own tenant.
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_insert ON audit_log FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
