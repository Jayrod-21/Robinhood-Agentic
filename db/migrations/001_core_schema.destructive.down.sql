-- 001_core_schema (down) — drop securities, data_sources, the runtime role, and the shared
-- trigger function.
--
-- Data loss: every security reference row and every provenance record. Reverse order of creation so
-- the FK from securities to data_sources is gone before its parent.

DROP TRIGGER IF EXISTS trg_securities_updated_at ON securities;
DROP TRIGGER IF EXISTS trg_data_sources_updated_at ON data_sources;

DROP TABLE IF EXISTS securities;
DROP TABLE IF EXISTS data_sources;

-- The role must lose its default-privilege entries and every grant before it can be dropped.
-- DROP OWNED BY does both for the current database (the role owns no objects — migrations run as
-- the DDL role, so DROP OWNED here only revokes).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rh_app') THEN
        DROP OWNED BY rh_app;
        DROP ROLE rh_app;
    END IF;
END
$$;

-- Dropped last: 002+ attach this trigger too, so rolling back 001 while a later migration is still
-- applied would strip a function they depend on. The runner rolls back in descending version order,
-- so by the time we are here those migrations are already reverted.
DROP FUNCTION IF EXISTS set_updated_at();
