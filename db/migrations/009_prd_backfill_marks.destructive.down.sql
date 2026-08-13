-- 009_prd_backfill_marks (down) — restore 004's unconditional 4-day mark window.
--
-- migrate: DESTRUCTIVE — DROP COLUMN mark_kind discards the live/backfill labels on every
-- existing row. Restoring the unconditional window then FAILS LOUDLY (ADD CONSTRAINT validates
-- existing rows) if any backfill mark priced outside trade_date + 4 days remains — such rows
-- cannot be represented under 004's constraint, and silently deleting them here would destroy
-- scored history. Remove or re-window those rows deliberately before rolling back.

ALTER TABLE portfolio_returns_daily
    DROP CONSTRAINT IF EXISTS ck_prd_mark_window;
ALTER TABLE portfolio_returns_daily
    DROP CONSTRAINT IF EXISTS ck_prd_mark_kind;
ALTER TABLE portfolio_returns_daily
    DROP COLUMN IF EXISTS mark_kind;

-- Verbatim 004 text (004_evaluation.up.sql, ck_prd_mark_window).
ALTER TABLE portfolio_returns_daily
    ADD CONSTRAINT ck_prd_mark_window CHECK (
        priced_as_of >= (trade_date::timestamp AT TIME ZONE 'UTC') AND
        priced_as_of <  ((trade_date + 4)::timestamp AT TIME ZONE 'UTC')
    );
