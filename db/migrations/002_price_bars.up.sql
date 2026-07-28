-- 002_price_bars — OHLCV bars, range-partitioned by month.
--
-- Sizing drove the design. A single Polygon day file holds ~1.44M rows (the full US equity
-- universe at minute resolution), so the archive on hand is ~300M rows for eleven months — and the
-- full 5-year set (2020-10-02 → 2025-10-02, in data/market/minute_bars_5y/) is roughly 1.6
-- billion. An unpartitioned table at that size makes every index rebuild, vacuum, and range scan
-- progressively worse, and makes dropping a bad load a 300M-row DELETE instead of a DROP.
--
-- Monthly RANGE partitions on ts:
--   * queries are almost always "this symbol over this window", which prunes to a few partitions;
--   * a bad day's load is reverted by dropping/rewriting one partition;
--   * autovacuum works per-partition instead of on one enormous heap.
--
-- There is NO DEFAULT partition, deliberately. An insert with no matching partition fails loudly
-- (Bar §0: fail loud), which is strictly better than the alternative: rows quietly accumulating in
-- a catch-all make every later partition creation scan the DEFAULT under ACCESS EXCLUSIVE and
-- REFUSE to attach once any resident row falls in the new range — a wedged ingest that needs
-- hand-surgery to unstick. Load through a staging table if a partial COPY abort is a concern.
--
-- Prices are NUMERIC, never float. SENIOR_ENGINEER_BAR §7.2: binary floating point silently
-- corrupts money — the error is small per row and unbounded once it compounds through a P&L.

CREATE TABLE IF NOT EXISTS price_bars_minute (
    security_id  BIGINT      NOT NULL,
    -- Bar OPEN time, UTC. Polygon supplies window_start as a nanosecond epoch; the loader converts.
    ts           TIMESTAMPTZ NOT NULL,
    open         NUMERIC(18, 6) NOT NULL,
    high         NUMERIC(18, 6) NOT NULL,
    low          NUMERIC(18, 6) NOT NULL,
    close        NUMERIC(18, 6) NOT NULL,
    volume       BIGINT      NOT NULL,
    transactions INTEGER,
    source_id    BIGINT,

    CONSTRAINT fk_price_bars_minute_security FOREIGN KEY (security_id)
        REFERENCES securities (id) ON DELETE RESTRICT,
    -- RESTRICT + deliberately UNINDEXED: data_sources is append-only by policy (see 001), so the
    -- only operation an index here would serve is a refused DELETE — and at 1.6B rows the index
    -- costs tens of GB. Documented deviation from Bar §4.1 "index every FK".
    CONSTRAINT fk_price_bars_minute_source FOREIGN KEY (source_id)
        REFERENCES data_sources (id) ON DELETE RESTRICT,

    -- Bar self-consistency. A bar violating these is corrupt on arrival, and letting it in means
    -- discovering it later as an inexplicable backtest result rather than a load error.
    -- (open BETWEEN low AND high on NOT NULL columns already implies high >= low, so no separate
    -- high/low CHECK is needed.)
    CONSTRAINT ck_price_bars_minute_ohlc   CHECK (open BETWEEN low AND high AND close BETWEEN low AND high),
    CONSTRAINT ck_price_bars_minute_pos    CHECK (low > 0),
    CONSTRAINT ck_price_bars_minute_volume CHECK (volume >= 0),
    CONSTRAINT ck_price_bars_minute_txn    CHECK (transactions IS NULL OR transactions >= 0),

    -- The partition key must be part of the primary key in a partitioned table. (security_id, ts)
    -- is also the natural uniqueness constraint and the index the hot query path wants.
    -- No created_at/updated_at, deliberately (documented deviation from Bar §4.3): bars are
    -- immutable bulk data — a re-load is delete+insert under a new source_id — and load provenance
    -- already lives in data_sources.fetched_at via source_id. Two timestamptz columns would cost
    -- ~26 GB at 1.6B rows for information already recorded once per file.
    PRIMARY KEY (security_id, ts)
) PARTITION BY RANGE (ts);

COMMENT ON TABLE price_bars_minute IS
    'Minute OHLCV, monthly RANGE partitions on ts, no DEFAULT partition — an out-of-range insert '
    'fails loudly. Prices NUMERIC — never float. ts is the bar OPEN time in UTC.';

-- Time-ordered scans across all symbols (e.g. "every bar on this day") would otherwise fall back to
-- the PK, which leads with security_id and cannot serve them.
CREATE INDEX IF NOT EXISTS ix_price_bars_minute_ts ON price_bars_minute (ts);

-- ── partition management ─────────────────────────────────────────────────────────────────────
-- Idempotent helper covering a RANGE of months, so the loader ensures partitions for the file's
-- ACTUAL timestamp span, not its nominal date. This matters: Polygon day files carry post-market
-- bars through 20:00 ET, and during EST (UTC−5) the 19:00–19:59 ET bars have UTC timestamps past
-- midnight — the 2020-11-30 file's late bars belong to 2020-12-01. A single-month "ensure the
-- file's date" contract loses those rows at every EST month-end; the loader MUST call this with
-- min(ts)..max(ts) of the file being loaded.
CREATE OR REPLACE FUNCTION ensure_price_bar_partitions(p_from DATE, p_to DATE) RETURNS TEXT[]
LANGUAGE plpgsql AS $$
DECLARE
    v_month DATE := date_trunc('month', p_from)::DATE;
    v_last  DATE := date_trunc('month', p_to)::DATE;
    v_name  TEXT;
    v_made  TEXT[] := '{}';
BEGIN
    IF p_to < p_from THEN
        RAISE EXCEPTION 'ensure_price_bar_partitions: p_to (%) before p_from (%)', p_to, p_from;
    END IF;
    -- A garbage timestamp (e.g. a misparsed epoch landing in 1970 or 2262) must not silently
    -- create thousands of partitions; 240 months is far beyond any legitimate single call.
    IF (extract(year FROM v_last) - extract(year FROM v_month)) * 12
       + (extract(month FROM v_last) - extract(month FROM v_month)) >= 240 THEN
        RAISE EXCEPTION 'ensure_price_bar_partitions: range % → % spans 240+ months — a corrupt timestamp, not a load', p_from, p_to;
    END IF;
    WHILE v_month <= v_last LOOP
        v_name := format('price_bars_minute_%s', to_char(v_month, 'YYYY_MM'));
        -- to_regclass returns NULL rather than raising when the relation is absent, making this
        -- safe to call on every load. Schema-qualified in both the check and the CREATE so a
        -- non-default search_path cannot create the partition somewhere to_regclass never looks.
        IF to_regclass(format('public.%I', v_name)) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE public.%I PARTITION OF public.price_bars_minute FOR VALUES FROM (%L) TO (%L)',
                v_name, v_month, (v_month + INTERVAL '1 month')::DATE
            );
        END IF;
        v_made := v_made || v_name;
        v_month := (v_month + INTERVAL '1 month')::DATE;
    END LOOP;
    RETURN v_made;
END;
$$;

COMMENT ON FUNCTION ensure_price_bar_partitions(DATE, DATE) IS
    'Idempotently create monthly partitions covering [p_from, p_to]. Call with the file''s actual '
    'min(ts)..max(ts) — post-market EST bars spill past UTC midnight into the next month. An insert '
    'with no matching partition raises (no DEFAULT partition exists).';

-- Pre-create the known archive window (2020-10 … 2025-11: the 5-year set ends 2025-10-02, plus one
-- month of headroom). 62 empty partitions cost nothing, and the bulk load can then never stall on
-- DDL mid-COPY; live ingest beyond this window creates months via the helper.
DO $$
BEGIN
    PERFORM public.ensure_price_bar_partitions(DATE '2020-10-01', DATE '2025-11-01');
END
$$;

-- ── daily bars ───────────────────────────────────────────────────────────────────────────────
-- Separate table, not a view over minutes. The evaluation framework computes Sharpe/Sortino on
-- DAILY returns, and aggregating 300M minute rows on every metric run would be absurd. Also the
-- natural home for the longer FMP history, which is daily and reaches back decades.
CREATE TABLE IF NOT EXISTS price_bars_daily (
    security_id  BIGINT      NOT NULL,
    trade_date   DATE        NOT NULL,
    open         NUMERIC(18, 6) NOT NULL,
    high         NUMERIC(18, 6) NOT NULL,
    low          NUMERIC(18, 6) NOT NULL,
    close        NUMERIC(18, 6) NOT NULL,
    -- Split/dividend adjusted close, when the provider supplies one. Kept beside the raw close:
    -- returns must be computed from adjusted prices, but orders were filled at raw ones.
    adj_close    NUMERIC(18, 6),
    volume       BIGINT      NOT NULL,
    source_id    BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- adj_close plausibly arrives by LATER UPDATE (provider backfills adjustments); updated_at +
    -- trigger make that visible instead of silent (Bar §4.3).
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_price_bars_daily_security FOREIGN KEY (security_id)
        REFERENCES securities (id) ON DELETE RESTRICT,
    -- RESTRICT + unindexed for the same reason as the minute table (data_sources is append-only).
    CONSTRAINT fk_price_bars_daily_source FOREIGN KEY (source_id)
        REFERENCES data_sources (id) ON DELETE RESTRICT,

    -- open/close BETWEEN low AND high implies high >= low (all NOT NULL), as on the minute table.
    CONSTRAINT ck_price_bars_daily_ohlc   CHECK (open BETWEEN low AND high AND close BETWEEN low AND high),
    CONSTRAINT ck_price_bars_daily_pos    CHECK (low > 0),
    CONSTRAINT ck_price_bars_daily_adj    CHECK (adj_close IS NULL OR adj_close > 0),
    CONSTRAINT ck_price_bars_daily_volume CHECK (volume >= 0),

    PRIMARY KEY (security_id, trade_date)
);

CREATE INDEX IF NOT EXISTS ix_price_bars_daily_date ON price_bars_daily (trade_date);

DROP TRIGGER IF EXISTS trg_price_bars_daily_updated_at ON price_bars_daily;
CREATE TRIGGER trg_price_bars_daily_updated_at
    BEFORE UPDATE ON price_bars_daily
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE price_bars_daily IS
    'Daily OHLCV. The return series every Sharpe/Sortino calculation reads — aggregating minute bars '
    'per metric run would not be viable.';
COMMENT ON COLUMN price_bars_daily.adj_close IS
    'Split/dividend adjusted. Returns use this; fills happened at the raw close.';
