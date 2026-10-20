-- Role rules inside a tenant.
--
-- Policies are PERMISSIVE by default: a row passes if ANY permissive policy
-- allows it. A RESTRICTIVE policy must ALSO pass. So a row is visible only if
--     tenant_isolation (004)  AND  role_access (this file).
--
-- app.role is set per transaction from the verified access token (app/db.py).
-- An unset or unknown role sees NOTHING, not even public documents: a request
-- that forgot its role should fail closed.

CREATE POLICY role_access ON chunks
    AS RESTRICTIVE
    USING (
        CASE NULLIF(current_setting('app.role', true), '')
            WHEN 'employee'   THEN classification = 'public'
            WHEN 'hr_manager' THEN classification IN ('public', 'hr_confidential')
            WHEN 'admin'      THEN classification IN ('public', 'hr_confidential', 'executive')
            ELSE false
        END
    );

CREATE POLICY role_access ON documents
    AS RESTRICTIVE
    USING (
        CASE NULLIF(current_setting('app.role', true), '')
            WHEN 'employee'   THEN classification = 'public'
            WHEN 'hr_manager' THEN classification IN ('public', 'hr_confidential')
            WHEN 'admin'      THEN classification IN ('public', 'hr_confidential', 'executive')
            ELSE false
        END
    );
