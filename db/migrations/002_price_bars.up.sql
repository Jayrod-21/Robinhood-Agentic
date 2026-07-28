-- 002_price_bars — OHLCV bars, range-partitioned by month.
--
-- migrate: non-destructive
--
-- Sizing drove the design. A single Polygon day file holds ~1.44M rows (the full US equity
-- universe at minute resolution), so the archive on hand is ~300M rows for eleven months — and
-- Jared has five years still to transfer, which is roughly 1.6 billion. An unpartitioned table at
-- that size makes every index rebuild, vacuum, and range scan progressively worse, and makes
-- dropping a bad load a 300M-row DELETE instead of a DROP.
--
-- Monthly RANGE partitions on ts:
--   * queries are almost always "this symbol over this window", which prunes to a few partitions;
--   * a bad day's load is reverted by dropping/rewriting one partition;
--   * autovacuum works per-partition instead of on one enormous heap.
--
-- Prices are NUMERIC, never float. SENIOR_ENGINEER_BAR §7.2: binary floating point silently
-- corrupts money — the error is small per row and unbounded once it compounds through a P&L.

CREATE TABLE IF NOT EXISTS price_bars_minute (
    security_id  BIGINT      NOT NULL REFERENCES securities (id) ON DELETE RESTRICT,
    -- Bar OPEN time, UTC. Polygon supplies window_start as a nanosecond epoch; the loader converts.
    ts           TIMESTAMPTZ NOT NULL,
    open         NUMERIC(18, 6) NOT NULL,
    high         NUMERIC(18, 6) NOT NULL,
    low          NUMERIC(18, 6) NOT NULL,
    close        NUMERIC(18, 6) NOT NULL,
    volume       BIGINT      NOT NULL,
    transactions INTEGER,
    source_id    BIGINT      REFERENCES data_sources (id) ON DELETE SET NULL,

    -- Bar self-consistency. A bar violating these is corrupt on arrival, and letting it in means
    -- discovering it later as an inexplicable backtest result rather than a load error.
    CONSTRAINT ck_price_bars_minute_hl     CHECK (high >= low),
    CONSTRAINT ck_price_bars_minute_ohlc   CHECK (open BETWEEN low AND high AND close BETWEEN low AND high),
    CONSTRAINT ck_price_bars_minute_pos    CHECK (low > 0),
    CONSTRAINT ck_price_bars_minute_volume CHECK (volume >= 0),
    CONSTRAINT ck_price_bars_minute_txn    CHECK (transactions IS NULL OR transactions >= 0),

    -- The partition key must be part of the primary key in a partitioned table. (security_id, ts)
    -- is also the natural uniqueness constraint and the index the hot query path wants.
    PRIMARY KEY (security_id, ts)
) PARTITION BY RANGE (ts);

COMMENT ON TABLE price_bars_minute IS
    'Minute OHLCV, monthly RANGE partitions on ts. Prices NUMERIC — never float. ts is the bar OPEN '
    'time in UTC.';

-- Time-ordered scans across all symbols (e.g. "every bar on this day") would otherwise fall back to
-- the PK, which leads with security_id and cannot serve them.
CREATE INDEX IF NOT EXISTS ix_price_bars_minute_ts ON price_bars_minute (ts);

-- ── partition management ─────────────────────────────────────────────────────────────────────
-- Idempotent helper so the loader can ensure a partition exists before writing a day, rather than
-- requiring a migration per month. Writing to a missing range raises rather than silently routing
-- elsewhere, so this must run first.
CREATE OR REPLACE FUNCTION ensure_price_bar_partition(p_month DATE) RETURNS TEXT
LANGUAGE plpgsql AS $$
DECLARE
    v_start DATE := date_trunc('month', p_month)::DATE;
    v_end   DATE := (date_trunc('month', p_month) + INTERVAL '1 month')::DATE;
    v_name  TEXT := format('price_bars_minute_%s', to_char(v_start, 'YYYY_MM'));
BEGIN
    -- to_regclass returns NULL rather than raising when the relation is absent, which makes this
    -- safe to call on every load without a duplicate-object error.
    IF to_regclass(format('public.%I', v_name)) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF price_bars_minute FOR VALUES FROM (%L) TO (%L)',
            v_name, v_start, v_end
        );
    END IF;
    RETURN v_name;
END;
$$;

COMMENT ON FUNCTION ensure_price_bar_partition(DATE) IS
    'Idempotently create the monthly partition covering p_month. Call before loading a day — an '
    'insert with no matching partition raises.';

-- A default partition catches rows outside every declared range instead of failing the insert.
-- Deliberate: a load with a bad timestamp should be visible and queryable afterwards, not an abort
-- halfway through a 1.4M-row COPY. Rows landing here indicate a loader bug and should be audited —
-- ix_price_bars_minute_ts makes that a cheap query.
CREATE TABLE IF NOT EXISTS price_bars_minute_default PARTITION OF price_bars_minute DEFAULT;

COMMENT ON TABLE price_bars_minute_default IS
    'Catch-all for timestamps outside every declared month. A non-empty count here means the loader '
    'produced out-of-range timestamps — investigate rather than ignore.';

-- ── daily bars ───────────────────────────────────────────────────────────────────────────────
-- Separate table, not a view over minutes. The evaluation framework computes Sharpe/Sortino on
-- DAILY returns, and aggregating 300M minute rows on every metric run would be absurd. Also the
-- natural home for the longer FMP history, which is daily and reaches back decades.
CREATE TABLE IF NOT EXISTS price_bars_daily (
    security_id  BIGINT      NOT NULL REFERENCES securities (id) ON DELETE RESTRICT,
    trade_date   DATE        NOT NULL,
    open         NUMERIC(18, 6) NOT NULL,
    high         NUMERIC(18, 6) NOT NULL,
    low          NUMERIC(18, 6) NOT NULL,
    close        NUMERIC(18, 6) NOT NULL,
    -- Split/dividend adjusted close, when the provider supplies one. Kept beside the raw close:
    -- returns must be computed from adjusted prices, but orders were filled at raw ones.
    adj_close    NUMERIC(18, 6),
    volume       BIGINT      NOT NULL,
    source_id    BIGINT      REFERENCES data_sources (id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_price_bars_daily_hl     CHECK (high >= low),
    CONSTRAINT ck_price_bars_daily_ohlc   CHECK (open BETWEEN low AND high AND close BETWEEN low AND high),
    CONSTRAINT ck_price_bars_daily_pos    CHECK (low > 0),
    CONSTRAINT ck_price_bars_daily_adj    CHECK (adj_close IS NULL OR adj_close > 0),
    CONSTRAINT ck_price_bars_daily_volume CHECK (volume >= 0),

    PRIMARY KEY (security_id, trade_date)
);

CREATE INDEX IF NOT EXISTS ix_price_bars_daily_date ON price_bars_daily (trade_date);

COMMENT ON TABLE price_bars_daily IS
    'Daily OHLCV. The return series every Sharpe/Sortino calculation reads — aggregating minute bars '
    'per metric run would not be viable.';
COMMENT ON COLUMN price_bars_daily.adj_close IS
    'Split/dividend adjusted. Returns use this; fills happened at the raw close.';
