-- 0.1.4: remember when each user last signed in.
--
-- EXPAND only: a new nullable column with no default, so the ALTER is instant and
-- the previous release (which never mentions it) keeps working while old and new
-- pods run side by side during a rolling upgrade. Never rename or drop a column in
-- the same release that stops using it: CONTRACT in a later release, once no pod
-- running the old code can exist.
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;
