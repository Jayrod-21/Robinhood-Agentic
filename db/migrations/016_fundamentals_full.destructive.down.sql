-- Reverse of 016. Destructive: drops every wider fundamental and the Piotroski working. Anything
-- ingested into these columns is lost and must be re-fetched. Back up first.
DROP INDEX IF EXISTS ix_fundamentals_piotroski;
ALTER TABLE fundamentals_snapshots
    DROP CONSTRAINT IF EXISTS ck_fundamentals_piotroski_variant,
    DROP CONSTRAINT IF EXISTS ck_fundamentals_piotroski_signals_obj,
    DROP CONSTRAINT IF EXISTS ck_fundamentals_derived_obj;
ALTER TABLE fundamentals_snapshots
    DROP COLUMN IF EXISTS dividend_yield, DROP COLUMN IF EXISTS ev_to_ebitda,
    DROP COLUMN IF EXISTS price_to_book, DROP COLUMN IF EXISTS price_to_sales,
    DROP COLUMN IF EXISTS price_to_tangible_book, DROP COLUMN IF EXISTS beta,
    DROP COLUMN IF EXISTS week_52_high, DROP COLUMN IF EXISTS week_52_low,
    DROP COLUMN IF EXISTS avg_volume_30d, DROP COLUMN IF EXISTS revenue_ttm,
    DROP COLUMN IF EXISTS ebitda_ttm, DROP COLUMN IF EXISTS capital_expenditure,
    DROP COLUMN IF EXISTS net_debt, DROP COLUMN IF EXISTS shares_outstanding,
    DROP COLUMN IF EXISTS tangible_book_value_per_share, DROP COLUMN IF EXISTS eps_growth_yoy,
    DROP COLUMN IF EXISTS rd_to_revenue, DROP COLUMN IF EXISTS equity_to_assets,
    DROP COLUMN IF EXISTS analyst_target_price, DROP COLUMN IF EXISTS analyst_recommendation,
    DROP COLUMN IF EXISTS derived_fields, DROP COLUMN IF EXISTS piotroski_variant,
    DROP COLUMN IF EXISTS piotroski_signals;
