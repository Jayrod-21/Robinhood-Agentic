-- 009_prd_backfill_marks — distinguish live marks from backfill marks on portfolio_returns_daily.
--
-- Issued for issue #33 (docs/fixpass/REVIEW_FIXES_004_2026-07-28.md), closed BEFORE the marking
-- job (#36) writes its first row — portfolio_returns_daily held 0 rows when this migration was
-- written, so this is still a schema fix rather than a data migration.
--
-- WHY
--   004's ck_prd_mark_window hard-coded priced_as_of into [trade_date, trade_date + 4 days) UTC.
--   That window permanently rejected every legitimate late mark: a marking job down over a long
--   weekend, and — the case the evaluation framework depends on — the historical re-mark of
--   counterfactual portfolios across the full price history (EVALUATION_FRAMEWORK.md §3.3), which
--   is by definition a mark whose priced_as_of is far later than its trade_date. There was no
--   tuning path; the only remedy was a new migration.
--
--   The real invariant was never "no mark may ever be old". It is "a LIVE mark must not use stale
--   prices". So the two cases become representable and honestly labelled:
--     * mark_kind = 'live'      — written by the near-real-time marking job. The 4-day upper
--                                 bound still applies: a live mark priced more than a long
--                                 weekend after its trading day used stale data.
--     * mark_kind = 'backfill'  — an explicitly declared historical mark. Any age is legal,
--                                 because the row SAYS it was computed after the fact; a leakage
--                                 audit reads the label instead of trusting a window.
--   The lower bound applies to BOTH kinds: a mark priced from before its own trading day began
--   (UTC-anchored, 003's idiom) is a forecast wearing a Sharpe no matter what it is called.
--
--   The default is 'live' on purpose: a marking job that forgets to declare itself gets the
--   STRICT window, so mislabelling can only fail loudly (an undeclared backfill dies on the
--   4-day bound) — it can never silently launder an old mark as a fresh one.
--
-- 004 is applied and checksum-locked, so the constraint is replaced here rather than edited
-- there. The table was empty at authoring time; if rows exist when this runs, the re-validated
-- constraint only WIDENS what 004 accepted, so existing rows cannot fail it.

ALTER TABLE portfolio_returns_daily
    ADD COLUMN IF NOT EXISTS mark_kind TEXT NOT NULL DEFAULT 'live';

ALTER TABLE portfolio_returns_daily
    DROP CONSTRAINT IF EXISTS ck_prd_mark_kind;
ALTER TABLE portfolio_returns_daily
    ADD CONSTRAINT ck_prd_mark_kind CHECK (mark_kind IN ('live', 'backfill'));

ALTER TABLE portfolio_returns_daily
    DROP CONSTRAINT IF EXISTS ck_prd_mark_window;
ALTER TABLE portfolio_returns_daily
    ADD CONSTRAINT ck_prd_mark_window CHECK (
        priced_as_of >= (trade_date::timestamp AT TIME ZONE 'UTC') AND
        (mark_kind = 'backfill' OR
         priced_as_of < ((trade_date + 4)::timestamp AT TIME ZONE 'UTC'))
    );

COMMENT ON COLUMN portfolio_returns_daily.mark_kind IS
    '''live'' = written near-real-time by the marking job (priced_as_of bounded to trade_date + 4 '
    'days by ck_prd_mark_window); ''backfill'' = explicitly declared historical mark (any '
    'priced_as_of at or after the trading day). The label is the honesty mechanism: a leakage '
    'audit trusts a live mark''s window and treats a backfill as after-the-fact by declaration. '
    'Defaults to ''live'' so an undeclared backfill fails the strict window instead of passing '
    'as fresh.';

COMMENT ON CONSTRAINT ck_prd_mark_window ON portfolio_returns_daily IS
    'Both kinds: priced_as_of on or after the trading day''s UTC start — a mark priced from '
    'before its own day is a forecast. Live marks only: priced_as_of < trade_date + 4 days '
    '(long weekend + late provider backfill); a backfill mark is exempt because it is labelled '
    'as historical. Replaced 004''s unconditional 4-day window (issue #33).';
