-- 002_price_bars (down) — drop the bar tables and the partition helper.
--
-- migrate: destructive
--
-- Data loss: every price bar, potentially hundreds of millions of rows. Dropping the parent drops
-- every attached partition with it, so the monthly children need no separate handling.

DROP TABLE IF EXISTS price_bars_daily;
DROP TABLE IF EXISTS price_bars_minute;  -- cascades to all partitions incl. _default

DROP FUNCTION IF EXISTS ensure_price_bar_partition(DATE);
