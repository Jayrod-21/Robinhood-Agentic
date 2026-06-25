-- =============================================================================
-- Market Data Pipeline — Convenience Views
-- Migration: 002_views.sql
--
-- These views join the three data tiers into the "full row" shape you'd
-- normally query. Use them for reporting and debate context injection —
-- never denormalize by copying columns back into the base tables.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Full snapshot view: Tier 1 + latest Tier 2 + latest Tier 3 per ticker
--
-- Returns one row per snapshot with all 50 Bloomberg fields materialized.
-- The fundamentals and analyst data come from the FK-linked rows, so this
-- view always reflects exactly what was in effect at snapshot time.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_equity_full AS
SELECT
    -- Identity (from tickers — single source of truth for symbol)
    t.symbol,
    t.name,
    t.sector,
    t.industry,
    t.is_bank,

    -- Snapshot metadata
    s.id                    AS snapshot_id,
    s.snapshot_time,
    s.session,
    s.source,

    -- Tier 1: price + price-derived
    s.price,
    s.market_cap,
    s.volume_today,
    s.volume_avg_30d,
    s.high_52w,
    s.low_52w,
    s.beta,
    s.pe_trailing,
    s.pe_forward,
    s.price_book,
    s.price_sales,
    s.ev_ebitda,
    s.peg_ratio,
    s.fcf_yield,
    s.dividend_yield,
    s.price_tangible_book,

    -- Tier 2: analyst (from linked row)
    a.analyst_target_price,
    a.analyst_recommendation,
    a.eps_next_year_est,
    a.short_interest_pct,

    -- Tier 3: fundamentals (from linked row — forward-filled)
    f.period_end_date       AS fundamentals_as_of,
    f.revenue_ttm,
    f.ebitda_ttm,
    f.free_cash_flow,
    f.capex,
    f.net_debt,
    f.eps_ttm,
    f.eps_growth_yoy,
    f.gross_margin,
    f.operating_margin,
    f.net_margin,
    f.ebitda_margin,
    f.revenue_growth_yoy,
    f.rd_revenue_ratio,
    f.shares_outstanding,
    f.current_ratio,
    f.quick_ratio,
    f.debt_to_equity,
    f.tangible_bv_per_share,
    f.roe,
    f.roc,
    f.ebitda_interest_coverage,
    f.piotroski_f_score,
    f.cash_conversion_cycle,

    -- Ownership (latest 13-F filing as of snapshot date)
    o.insider_pct           AS insider_ownership_pct,
    o.institutional_pct     AS institutional_ownership_pct,

    -- Bank-specific (NULL for non-bank tickers)
    b.net_interest_margin,
    b.equity_assets_pct,
    b.efficiency_ratio,
    b.loan_loss_provision,

    -- Lineage FKs (for auditing which rows were used)
    s.fundamentals_id,
    s.analyst_snapshot_id

FROM equity_snapshots s
JOIN tickers                        t  ON t.id               = s.ticker_id
LEFT JOIN equity_fundamentals       f  ON f.id               = s.fundamentals_id
LEFT JOIN equity_analyst_snapshots  a  ON a.id               = s.analyst_snapshot_id
LEFT JOIN equity_fundamentals_banking b ON b.fundamentals_id  = s.fundamentals_id
LEFT JOIN LATERAL (
    SELECT insider_pct, institutional_pct
    FROM equity_ownership
    WHERE ticker_id = s.ticker_id
      AND filed_date <= s.snapshot_time::date
    ORDER BY filed_date DESC
    LIMIT 1
) o ON TRUE;

COMMENT ON VIEW v_equity_full IS 'All 50 Bloomberg fields per snapshot. Joins all data-tier tables including the split ownership and banking tables. Use for reporting and debate context injection.';


-- ---------------------------------------------------------------------------
-- Latest snapshot per ticker (most recent row regardless of session)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_equity_latest AS
SELECT DISTINCT ON (s.ticker_id) *
FROM v_equity_full s
ORDER BY s.ticker_id, s.snapshot_time DESC;

COMMENT ON VIEW v_equity_latest IS 'Most recent snapshot per ticker. Use for watchlist dashboards and scan inputs.';


-- ---------------------------------------------------------------------------
-- Today''s news with all affected tickers (denormalized for easy injection)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_news_today AS
SELECT
    e.email_date,
    ns.id           AS story_id,
    ns.rank,
    ns.headline,
    ns.source_name,
    ns.impact_score,
    ns.hype_score,
    ns.summary,
    ns.sentiment,
    ns.is_macro,
    -- Aggregate tickers back into an array for easy consumption
    ARRAY_AGG(DISTINCT nst.symbol ORDER BY nst.symbol) AS tickers_affected,
    ARRAY_AGG(DISTINCT sec.sector  ORDER BY sec.sector)  AS sectors_affected
FROM market_mover_emails   e
JOIN news_stories          ns  ON ns.email_id  = e.id
LEFT JOIN news_story_tickers nst ON nst.story_id = ns.id
LEFT JOIN news_story_sectors sec ON sec.story_id = ns.id
WHERE e.email_date = CURRENT_DATE
GROUP BY e.email_date, ns.id, ns.rank, ns.headline, ns.source_name,
         ns.impact_score, ns.hype_score, ns.summary, ns.sentiment, ns.is_macro
ORDER BY ns.rank;

COMMENT ON VIEW v_news_today IS 'Today''s news stories with tickers/sectors aggregated. Feed directly into debate context builder.';


-- ---------------------------------------------------------------------------
-- News stories relevant to a specific ticker (any date)
-- Usage:  SELECT * FROM v_news_for_ticker WHERE symbol = 'AMD' AND email_date >= CURRENT_DATE - 7;
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_news_for_ticker AS
SELECT
    t.symbol,
    e.email_date,
    ns.rank,
    ns.headline,
    ns.impact_score,
    ns.hype_score,
    ns.summary,
    ns.sentiment,
    ns.is_macro,
    ns.article_url
FROM news_story_tickers    nst
JOIN news_stories          ns  ON ns.id       = nst.story_id
JOIN market_mover_emails   e   ON e.id        = ns.email_id
-- Left join to tickers so we surface stories for symbols not in our watchlist too
LEFT JOIN tickers          t   ON t.symbol    = nst.symbol
ORDER BY e.email_date DESC, ns.rank;

COMMENT ON VIEW v_news_for_ticker IS 'All news stories for a given symbol. Filter by symbol and email_date. Includes symbols not in our watchlist.';


-- ---------------------------------------------------------------------------
-- Daily summary with all three sessions side by side
--
-- One row per (ticker, date). Pre-computed intraday metrics plus the
-- symbol from tickers. Primary input for the debate model's rolling
-- context window and the UI trend table.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_daily_summary AS
SELECT
    t.symbol,
    t.name,
    t.sector,
    ds.trading_date,

    -- Session prices
    ds.open_price,
    ds.midday_price,
    ds.close_price,

    -- Session volumes
    ds.open_volume,
    ds.midday_volume,
    ds.close_volume,

    -- Intraday derived metrics
    ds.intraday_high,
    ds.intraday_low,
    ds.intraday_range_pct,
    ds.open_to_close_pct,
    ds.open_to_midday_pct,
    ds.midday_to_close_pct,

    -- Fundamentals in effect that day (for model context)
    f.pe_trailing,
    f.revenue_ttm,
    f.ebitda_ttm,
    f.gross_margin,
    f.piotroski_f_score,

    -- Analyst data in effect that day
    a.analyst_recommendation,
    a.analyst_target_price,
    a.short_interest_pct,

    -- Lineage
    ds.open_snapshot_id,
    ds.midday_snapshot_id,
    ds.close_snapshot_id,
    ds.fundamentals_id

FROM equity_daily_summary       ds
JOIN tickers                    t  ON t.id  = ds.ticker_id
LEFT JOIN equity_fundamentals   f  ON f.id  = ds.fundamentals_id
LEFT JOIN equity_analyst_snapshots a ON a.id = ds.analyst_snapshot_id
ORDER BY ds.trading_date DESC, t.symbol;

COMMENT ON VIEW v_daily_summary IS 'All three sessions per ticker per day with intraday metrics pre-computed. Primary view for the debate model rolling window and UI trend tables.';
