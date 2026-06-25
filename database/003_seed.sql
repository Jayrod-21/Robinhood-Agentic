-- =============================================================================
-- Market Data Pipeline — Seed Data
-- Migration: 003_seed.sql
--
-- Development seed: insert AMD for testing.
-- Production watchlist will be loaded separately via the pipeline.
-- =============================================================================

INSERT INTO tickers (symbol, name, sector, industry, is_bank, active)
VALUES
    ('AMD',  'Advanced Micro Devices, Inc.', 'Electronic Technology',   'Semiconductors',                    FALSE, TRUE),
    ('SPY',  'SPDR S&P 500 ETF Trust',       'Miscellaneous',           'Exchange-Traded Fund',              FALSE, TRUE),
    ('QQQ',  'Invesco QQQ Trust',             'Miscellaneous',           'Exchange-Traded Fund',              FALSE, TRUE)
ON CONFLICT (symbol) DO NOTHING;
