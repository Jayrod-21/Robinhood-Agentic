-- 005_corporate_actions (down) — drop the actions table and the adjustment function.
--
-- Data loss: every recorded split and dividend. Re-fetchable from the provider, but any
-- provider-supplied announcement date not re-obtainable would be lost.
--
-- adj_close values already written into price_bars_daily are NOT reverted here: they live in 002's
-- table, this migration only computed them, and blanking a column 002 owns would be this migration
-- reaching outside its own scope. They become stale-but-harmless numbers whose provenance is gone —
-- which is exactly why the backfill is re-runnable and idempotent.

DROP FUNCTION IF EXISTS split_factor_after(BIGINT, DATE);

DROP TRIGGER IF EXISTS trg_corporate_actions_updated_at ON corporate_actions;
DROP TABLE IF EXISTS corporate_actions;
