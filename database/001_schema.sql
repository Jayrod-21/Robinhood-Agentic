-- =============================================================================
-- Market Data Pipeline — PostgreSQL Schema
-- Migration: 001_schema.sql
--
-- Design principles:
--   • Every non-root table has a FK to its parent. No orphaned rows.
--   • Symbol (ticker) is defined exactly once: tickers.symbol.
--     All other tables reference tickers(id). Never store a bare symbol
--     string without a FK — the only exception is junction tables that
--     intentionally allow symbols outside our watchlist (news mentions).
--   • Data separated by update cadence into three tables:
--       equity_snapshots         — Tier 1: 3× daily, price + price-derived ratios
--       equity_analyst_snapshots — Tier 2: weekly, analyst estimates + short interest
--       equity_fundamentals      — Tier 3: quarterly, financial statement data
--     equity_snapshots holds FKs to the Tier 2/3 rows it was computed against,
--     so forward-fill is a JOIN, not duplicated column values.
--   • Many-to-many relationships use explicit junction tables with composite PKs.
--   • CHECK constraints enforce bounded values at the DB layer.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- EXTENSIONS
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- trigram indexes for text search


-- ---------------------------------------------------------------------------
-- REFERENCE DATA
-- ---------------------------------------------------------------------------

CREATE TABLE tickers (
    id          SERIAL          PRIMARY KEY,
    symbol      VARCHAR(10)     NOT NULL UNIQUE,
    name        VARCHAR(200),
    sector      VARCHAR(100),
    industry    VARCHAR(100),
    is_bank     BOOLEAN         NOT NULL DEFAULT FALSE,
    active      BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  tickers IS 'Master watchlist. Single source of truth for every symbol in the system.';
COMMENT ON COLUMN tickers.is_bank IS 'TRUE enables banking-specific columns in equity_fundamentals (NIM, efficiency ratio, etc.).';


-- ---------------------------------------------------------------------------
-- MARKET DATA — Tier 3: quarterly financial statement data
--
-- One row per (ticker, fiscal quarter end). Covers the full set of
-- Bloomberg fields that only change when a company reports earnings.
-- All tables that need quarterly data reference this table via FK —
-- no column values are copied/repeated elsewhere.
-- ---------------------------------------------------------------------------

CREATE TABLE equity_fundamentals (
    id                          BIGSERIAL       PRIMARY KEY,
    ticker_id                   INTEGER         NOT NULL REFERENCES tickers(id),
    period_end_date             DATE            NOT NULL,
    reported_at                 TIMESTAMPTZ,                   -- when earnings were released

    -- Income & profitability
    revenue_ttm                 BIGINT,
    ebitda_ttm                  BIGINT,
    free_cash_flow              BIGINT,
    capex                       BIGINT,
    net_debt                    BIGINT,
    eps_ttm                     NUMERIC(10,4),
    eps_growth_yoy              NUMERIC(8,4),
    gross_margin                NUMERIC(8,4),
    operating_margin            NUMERIC(8,4),
    net_margin                  NUMERIC(8,4),
    ebitda_margin               NUMERIC(8,4),
    revenue_growth_yoy          NUMERIC(8,4),
    rd_revenue_ratio            NUMERIC(8,4),

    -- Balance sheet health
    shares_outstanding          BIGINT,
    current_ratio               NUMERIC(8,4),
    quick_ratio                 NUMERIC(8,4),
    debt_to_equity              NUMERIC(10,4),
    tangible_bv_per_share       NUMERIC(12,4),

    -- Returns & coverage
    roe                         NUMERIC(8,4),
    roc                         NUMERIC(8,4),
    ebitda_interest_coverage    NUMERIC(10,4),

    -- Composite quality scores (calculated from raw financials)
    piotroski_f_score           SMALLINT        CHECK (piotroski_f_score BETWEEN 0 AND 9),
    cash_conversion_cycle       NUMERIC(10,2),

    fmp_raw                     JSONB,
    created_at                  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (ticker_id, period_end_date)
);

COMMENT ON TABLE  equity_fundamentals IS 'Quarterly financial statement data. One row per ticker per earnings release. Forward-fill = read the latest row. Ownership and bank-specific data live in separate tables.';
COMMENT ON COLUMN equity_fundamentals.period_end_date IS 'Fiscal quarter end date (not the report date).';

CREATE INDEX idx_fund_ticker_period ON equity_fundamentals (ticker_id, period_end_date DESC);


-- ---------------------------------------------------------------------------
-- Ownership data — split from equity_fundamentals
--
-- Sourced from SEC 13-F filings, not earnings releases. Has its own
-- filing cadence and data source, so it lives in its own table.
-- One row per (ticker, filed_date).
-- ---------------------------------------------------------------------------

CREATE TABLE equity_ownership (
    id                      BIGSERIAL       PRIMARY KEY,
    ticker_id               INTEGER         NOT NULL REFERENCES tickers(id),
    filed_date              DATE            NOT NULL,
    insider_pct             NUMERIC(8,4)    CHECK (insider_pct BETWEEN 0 AND 100),
    institutional_pct       NUMERIC(8,4)    CHECK (institutional_pct BETWEEN 0 AND 100),
    fmp_raw                 JSONB,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (ticker_id, filed_date)
);

COMMENT ON TABLE equity_ownership IS 'SEC 13-F insider and institutional ownership. Separate from earnings-driven fundamentals — different source, different cadence.';

CREATE INDEX idx_ownership_ticker_date ON equity_ownership (ticker_id, filed_date DESC);


-- ---------------------------------------------------------------------------
-- Bank-specific fundamentals — split from equity_fundamentals
--
-- 1:1 with equity_fundamentals. Only rows where tickers.is_bank = TRUE
-- will have a corresponding row here. Keeps the main fundamentals table
-- free of sparse columns that are NULL for 95%+ of tickers.
-- ---------------------------------------------------------------------------

CREATE TABLE equity_fundamentals_banking (
    fundamentals_id     BIGINT          PRIMARY KEY REFERENCES equity_fundamentals(id) ON DELETE CASCADE,
    net_interest_margin NUMERIC(8,4),
    equity_assets_pct   NUMERIC(8,4)    CHECK (equity_assets_pct BETWEEN 0 AND 100),
    efficiency_ratio    NUMERIC(8,4),
    loan_loss_provision BIGINT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE equity_fundamentals_banking IS '1:1 extension of equity_fundamentals for bank-specific metrics. Only populated when tickers.is_bank = TRUE.';


-- ---------------------------------------------------------------------------
-- MARKET DATA — Tier 2: analyst estimates + short interest (weekly)
--
-- One row per (ticker, fetch date). Analyst data changes when analysts
-- publish updates — not tied to earnings releases.
-- ---------------------------------------------------------------------------

CREATE TABLE equity_analyst_snapshots (
    id                      BIGSERIAL       PRIMARY KEY,
    ticker_id               INTEGER         NOT NULL REFERENCES tickers(id),
    fetched_date            DATE            NOT NULL,

    analyst_target_price    NUMERIC(12,4),
    analyst_recommendation  VARCHAR(20),
    eps_next_year_est       NUMERIC(10,4),
    short_interest_pct      NUMERIC(8,4)    CHECK (short_interest_pct BETWEEN 0 AND 100),

    fmp_raw                 JSONB,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (ticker_id, fetched_date)
);

COMMENT ON TABLE equity_analyst_snapshots IS 'Weekly pull of analyst estimates and short interest. One row per ticker per fetch date.';

CREATE INDEX idx_analyst_ticker_date ON equity_analyst_snapshots (ticker_id, fetched_date DESC);


-- ---------------------------------------------------------------------------
-- MARKET DATA — Tier 1: 3× daily price snapshots
--
-- Stores live price + all price-derived ratios. The two FKs
-- (fundamentals_id, analyst_snapshot_id) point to the exact rows used to
-- compute the ratios, enabling full lineage tracing without copying any
-- column values from those tables.
--
-- Forward-fill logic lives in the application layer:
--   fundamentals_id  → set to the latest equity_fundamentals row for this ticker
--   analyst_snapshot_id → set to the latest equity_analyst_snapshots row
-- ---------------------------------------------------------------------------

CREATE TABLE equity_snapshots (
    id                  BIGSERIAL       PRIMARY KEY,
    ticker_id           INTEGER         NOT NULL REFERENCES tickers(id),
    snapshot_time       TIMESTAMPTZ     NOT NULL,
    session             VARCHAR(10)     NOT NULL CHECK (session IN ('open', 'midday', 'close')),

    -- Price & volume
    price               NUMERIC(12,4)   NOT NULL,
    market_cap          BIGINT,
    volume_today        BIGINT,
    volume_avg_30d      BIGINT,
    high_52w            NUMERIC(12,4),
    low_52w             NUMERIC(12,4),
    beta                NUMERIC(8,4),

    -- Price-derived ratios
    -- Numerator = price; denominator lives in the fundamentals/analyst rows below.
    pe_trailing         NUMERIC(10,4),
    pe_forward          NUMERIC(10,4),
    price_book          NUMERIC(10,4),
    price_sales         NUMERIC(10,4),
    ev_ebitda           NUMERIC(10,4),
    peg_ratio           NUMERIC(10,4),
    fcf_yield           NUMERIC(8,4),
    dividend_yield      NUMERIC(8,4),
    price_tangible_book NUMERIC(10,4),

    -- Lineage: which Tier 3 / Tier 2 rows were used to compute the ratios above
    fundamentals_id     BIGINT          REFERENCES equity_fundamentals(id),
    analyst_snapshot_id BIGINT          REFERENCES equity_analyst_snapshots(id),

    source              VARCHAR(20)     NOT NULL DEFAULT 'fmp',
    fmp_raw             JSONB,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (ticker_id, snapshot_time)
);

COMMENT ON TABLE  equity_snapshots IS '3× daily price snapshot. Ratios are computed from the linked fundamentals/analyst rows — never duplicate column values from those tables.';
COMMENT ON COLUMN equity_snapshots.fundamentals_id IS 'The equity_fundamentals row whose TTM data was used to compute trailing ratios at snapshot time. NULL if no quarterly data exists yet.';
COMMENT ON COLUMN equity_snapshots.analyst_snapshot_id IS 'The equity_analyst_snapshots row in effect at snapshot time. NULL if no analyst data exists yet.';

CREATE INDEX idx_snap_ticker_time   ON equity_snapshots (ticker_id, snapshot_time DESC);
CREATE INDEX idx_snap_session_time  ON equity_snapshots (session, snapshot_time DESC);


-- ---------------------------------------------------------------------------
-- MARKET DATA — Daily summary (written once after the close snapshot)
--
-- One row per (ticker, trading_date). Consolidates all three sessions
-- into a single row with FKs back to the individual snapshot rows.
-- Derived intraday metrics (momentum, range, reversal) are pre-computed
-- here so the debate model and UI can read one row instead of assembling
-- three. Written by the pipeline after the close job runs.
-- ---------------------------------------------------------------------------

CREATE TABLE equity_daily_summary (
    id                      BIGSERIAL       PRIMARY KEY,
    ticker_id               INTEGER         NOT NULL REFERENCES tickers(id),
    trading_date            DATE            NOT NULL,

    -- FK to each session's snapshot row
    open_snapshot_id        BIGINT          REFERENCES equity_snapshots(id),
    midday_snapshot_id      BIGINT          REFERENCES equity_snapshots(id),
    close_snapshot_id       BIGINT          REFERENCES equity_snapshots(id),

    -- Prices at each session
    open_price              NUMERIC(12,4),
    midday_price            NUMERIC(12,4),
    close_price             NUMERIC(12,4),

    -- Volume at each session
    open_volume             BIGINT,
    midday_volume           BIGINT,
    close_volume            BIGINT,

    -- Intraday derived metrics (pre-computed for model + UI consumption)
    intraday_high           NUMERIC(12,4),  -- max across the three session prices
    intraday_low            NUMERIC(12,4),  -- min across the three session prices
    intraday_range_pct      NUMERIC(8,4),   -- (high - low) / open * 100
    open_to_close_pct       NUMERIC(8,4),   -- (close - open) / open * 100
    open_to_midday_pct      NUMERIC(8,4),   -- (midday - open) / open * 100
    midday_to_close_pct     NUMERIC(8,4),   -- (close - midday) / midday * 100

    -- FK to the fundamentals row in effect that day (for model context)
    fundamentals_id         BIGINT          REFERENCES equity_fundamentals(id),
    analyst_snapshot_id     BIGINT          REFERENCES equity_analyst_snapshots(id),

    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (ticker_id, trading_date)
);

COMMENT ON TABLE  equity_daily_summary IS 'One row per ticker per trading day. All three sessions side by side with pre-computed intraday metrics. Written after the close snapshot. Primary input for debate model context and UI trend views.';
COMMENT ON COLUMN equity_daily_summary.open_to_close_pct    IS 'Full-day move: (close - open) / open * 100. Negative = down day.';
COMMENT ON COLUMN equity_daily_summary.open_to_midday_pct   IS 'Morning leg: tells the model whether the stock moved early or late.';
COMMENT ON COLUMN equity_daily_summary.midday_to_close_pct  IS 'Afternoon leg: gap-up-and-hold vs gap-up-and-fade shows up here.';

CREATE INDEX idx_daily_ticker_date ON equity_daily_summary (ticker_id, trading_date DESC);
CREATE INDEX idx_daily_date        ON equity_daily_summary (trading_date DESC);


-- ---------------------------------------------------------------------------
-- NEWS — Market Mover emails (one email = one trading day)
-- ---------------------------------------------------------------------------

CREATE TABLE market_mover_emails (
    id              BIGSERIAL       PRIMARY KEY,
    email_date      DATE            NOT NULL UNIQUE,   -- the trading day the email covers
    received_at     TIMESTAMPTZ,
    gmail_thread_id VARCHAR(200)    UNIQUE,
    raw_text        TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE market_mover_emails IS 'One row per Market Mover email. All news sub-tables cascade from here.';
COMMENT ON COLUMN market_mover_emails.email_date IS 'The trading day this email covers, not the wall-clock receive time.';


-- Top 3 (or more) market-moving stories per email
CREATE TABLE news_stories (
    id           BIGSERIAL       PRIMARY KEY,
    email_id     BIGINT          NOT NULL REFERENCES market_mover_emails(id) ON DELETE CASCADE,
    rank         SMALLINT        NOT NULL CHECK (rank >= 1),
    headline     TEXT            NOT NULL,
    source_name  VARCHAR(200),
    article_url  TEXT,
    impact_score NUMERIC(4,2)    CHECK (impact_score BETWEEN 0 AND 10),
    hype_score   NUMERIC(4,2)    CHECK (hype_score BETWEEN 0 AND 10),
    summary      TEXT,
    sentiment    VARCHAR(10)     CHECK (sentiment IN ('bullish', 'bearish', 'neutral')),
    is_macro     BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (email_id, rank)
);

COMMENT ON COLUMN news_stories.is_macro IS 'TRUE when impact_score >= 8.0 — injected as macro context into ALL debates that day, not just ticker-specific ones.';

CREATE INDEX idx_news_stories_email ON news_stories (email_id);


-- Junction: which tickers does a story affect?
-- No FK to tickers intentionally — news can mention symbols outside our watchlist.
-- Use the index on symbol to join against tickers when needed.
CREATE TABLE news_story_tickers (
    story_id    BIGINT          NOT NULL REFERENCES news_stories(id) ON DELETE CASCADE,
    symbol      VARCHAR(10)     NOT NULL,
    PRIMARY KEY (story_id, symbol)
);

CREATE INDEX idx_nst_symbol ON news_story_tickers (symbol);

-- Junction: which sectors does a story affect?
CREATE TABLE news_story_sectors (
    story_id    BIGINT          NOT NULL REFERENCES news_stories(id) ON DELETE CASCADE,
    sector      VARCHAR(100)    NOT NULL,
    PRIMARY KEY (story_id, sector)
);


-- Bear case section (one per email)
CREATE TABLE news_bear_cases (
    id          BIGSERIAL       PRIMARY KEY,
    email_id    BIGINT          NOT NULL REFERENCES market_mover_emails(id) ON DELETE CASCADE,
    headline    TEXT,
    summary     TEXT,
    source_url  TEXT,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (email_id)   -- one bear case per email
);

CREATE TABLE news_bear_case_tickers (
    bear_case_id    BIGINT          NOT NULL REFERENCES news_bear_cases(id) ON DELETE CASCADE,
    symbol          VARCHAR(10)     NOT NULL,
    PRIMARY KEY (bear_case_id, symbol)
);

CREATE INDEX idx_nbct_symbol ON news_bear_case_tickers (symbol);


-- Scorecard: how did yesterday's predictions perform?
-- original_story_id is nullable: the story being graded may pre-date our DB.
CREATE TABLE news_scorecard (
    id                  BIGSERIAL       PRIMARY KEY,
    email_id            BIGINT          NOT NULL REFERENCES market_mover_emails(id) ON DELETE CASCADE,
    original_story_id   BIGINT          REFERENCES news_stories(id),
    rank                SMALLINT        NOT NULL CHECK (rank >= 1),
    original_headline   TEXT            NOT NULL,
    impact_score        NUMERIC(4,2)    CHECK (impact_score BETWEEN 0 AND 10),
    ticker              VARCHAR(10),
    ticker_return_pct   NUMERIC(8,4),
    spy_return_pct      NUMERIC(8,4),
    vix_change_pct      NUMERIC(8,4),
    result              VARCHAR(10)     CHECK (result IN ('HIT', 'MISS', 'N/A')),
    notes               TEXT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (email_id, rank)
);

COMMENT ON COLUMN news_scorecard.original_story_id IS 'FK to the story being graded. NULL when the story predates our DB or was not stored.';
COMMENT ON COLUMN news_scorecard.ticker IS 'Ticker the prediction was about. Stored directly here (not FK) since it may not be in our watchlist.';


-- Earnings calendar sourced from the news email.
-- ticker_id is nullable: company may not be in our watchlist.
-- symbol is stored directly for tickers outside our watchlist.
CREATE TABLE earnings_from_news (
    id               BIGSERIAL       PRIMARY KEY,
    email_id         BIGINT          NOT NULL REFERENCES market_mover_emails(id) ON DELETE CASCADE,
    ticker_id        INTEGER         REFERENCES tickers(id),
    symbol           VARCHAR(10)     NOT NULL,
    report_date      DATE            NOT NULL,
    report_time      VARCHAR(20)     CHECK (report_time IN ('before_open', 'after_close', 'tbd')),
    eps_estimate     NUMERIC(10,4),
    rev_estimate_raw VARCHAR(50),    -- original string from email e.g. "$60.3B"
    rev_estimate     BIGINT,         -- parsed to cents/dollars
    created_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (email_id, symbol)
);

CREATE INDEX idx_efn_report_date ON earnings_from_news (report_date);
CREATE INDEX idx_efn_ticker      ON earnings_from_news (ticker_id);

COMMENT ON COLUMN earnings_from_news.ticker_id IS 'NULL when the symbol is not in our tickers watchlist.';
COMMENT ON COLUMN earnings_from_news.rev_estimate_raw IS 'Original string from the email before numeric parsing, e.g. "$60.3B".';


-- ---------------------------------------------------------------------------
-- UPDATED_AT trigger (applied to tickers)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tickers_updated_at
    BEFORE UPDATE ON tickers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
