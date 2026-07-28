-- 003_fundamentals (down) — drop the fundamentals snapshot table.
--
-- migrate: destructive
--
-- Data loss: every fundamentals reading, including the unparsed-cell provenance. Re-ingest from
-- data/market/ and the provider is possible but not free — the Bloomberg export is a fixed
-- four-day sample that cannot be re-pulled.

DROP TRIGGER IF EXISTS trg_fundamentals_updated_at ON fundamentals_snapshots;
DROP TABLE IF EXISTS fundamentals_snapshots;
