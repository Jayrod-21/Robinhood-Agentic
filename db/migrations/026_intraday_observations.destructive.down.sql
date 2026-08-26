-- Destructive by FILENAME: it drops two tables.
--
-- Sixth outing for "it is only derived data" (014, 022, 023, 024, 025). It is the weakest here and
-- still wrong. The ratios are recomputable from price and the linked statement row — but the PRICE
-- observations are not. A 30-minute intraday series cannot be rebuilt after the fact from any
-- source this project has: price_bars_daily is one bar a day, and the provider's intraday history
-- is not free. Every row dropped here is a measurement that cannot be taken again.
DROP TABLE IF EXISTS intraday_observations;
DROP TABLE IF EXISTS intraday_collection_runs;
