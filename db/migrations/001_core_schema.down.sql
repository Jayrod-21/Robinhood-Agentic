-- 001_core_schema (down) — drop securities, data_sources, and the shared trigger function.
--
-- migrate: destructive
--
-- Data loss: every security reference row and every provenance record. Reverse order of creation so
-- the FK from securities to data_sources is gone before its parent.

DROP TRIGGER IF EXISTS trg_securities_updated_at ON securities;

DROP TABLE IF EXISTS securities;
DROP TABLE IF EXISTS data_sources;

-- Dropped last: 002+ attach this trigger too, so rolling back 001 while a later migration is still
-- applied would strip a function they depend on. The runner rolls back in descending version order,
-- so by the time we are here those migrations are already reverted.
DROP FUNCTION IF EXISTS set_updated_at();
