-- Memory Vault — least-privilege database roles
--
-- Until now one database user did everything: created the schema at start-up
-- and then served every request. A bug or an injection reaching that
-- connection had DDL rights over the whole database, so "drop the chunks
-- table" was in range of the same credential that answers /api/search.
--
-- This migration defines three NOLOGIN group roles and grants each only what
-- its job needs:
--
--   memory_vault_app        DML on the application tables. No DDL.
--   memory_vault_readonly   SELECT only. For dashboards, backups, and psql
--                           sessions that have no business writing.
--   memory_vault_migrator   DDL. Used only while migrations run.
--
-- They are GROUP roles, not login users. Nothing here creates a user, sets a
-- password, or changes who the application connects as: an operator opts in
-- by creating a login role and granting it one of these. A deployment that
-- ignores this migration entirely keeps working exactly as it does today,
-- which is the point — this must not be able to lock anyone out of their own
-- database.
--
-- Adopting it is documented in docs/threat-model.md.

DO $$
BEGIN
    -- CREATE ROLE has no IF NOT EXISTS, and this migration has to be safe to
    -- run against a database where an operator already created these.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memory_vault_app') THEN
        CREATE ROLE memory_vault_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memory_vault_readonly') THEN
        CREATE ROLE memory_vault_readonly NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memory_vault_migrator') THEN
        CREATE ROLE memory_vault_migrator NOLOGIN;
    END IF;
END
$$;

-- Schema visibility. USAGE lets a role resolve names inside the schema; it
-- does not grant access to anything in it.
GRANT USAGE ON SCHEMA public TO memory_vault_app, memory_vault_readonly;
GRANT USAGE, CREATE ON SCHEMA public TO memory_vault_migrator;

-- Application role: read and write rows, never change their shape.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO memory_vault_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO memory_vault_app;

-- Read-only role.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO memory_vault_readonly;

-- Migrator role: everything, because it creates the objects the others use.
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO memory_vault_migrator;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO memory_vault_migrator;

-- The GRANTs above cover tables that exist right now. Default privileges make
-- the same true for tables a FUTURE migration creates, so adding a table does
-- not silently leave the app role unable to read it. These apply to objects
-- created by the role running this statement, which is why the migrator is the
-- role that should run migrations from here on.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO memory_vault_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO memory_vault_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO memory_vault_readonly;
