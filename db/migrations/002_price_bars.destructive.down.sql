-- 002_price_bars (down) — drop the bar tables and the partition helper.
--
-- Data loss: every price bar, potentially hundreds of millions of rows. Dropping the parent drops
-- every attached partition with it, so the monthly children need no separate handling.

DROP TRIGGER IF EXISTS trg_price_bars_daily_updated_at ON price_bars_daily;

DROP TABLE IF EXISTS price_bars_daily;
DROP TABLE IF EXISTS price_bars_minute;  -- cascades to all monthly partitions

DROP FUNCTION IF EXISTS ensure_price_bar_partitions(DATE, DATE);
