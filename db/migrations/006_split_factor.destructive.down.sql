-- 006_split_factor (down) — drop the factor column and narrow adj_close back.
--
-- migrate: DESTRUCTIVE, and in a way worth stating plainly. Narrowing adj_close to NUMERIC(18,6)
-- FAILS if any row holds a value the narrower type cannot represent — which is precisely the
-- situation 006 exists to handle. Roll this back only after re-running the adjustment with the
-- pathological rows cleared, or the ALTER will error.
--
-- Data loss: every split_adj_factor value. Recomputable by re-running the adjustment pass.

ALTER TABLE price_bars_daily DROP CONSTRAINT IF EXISTS ck_price_bars_daily_factor;
ALTER TABLE price_bars_daily DROP COLUMN IF EXISTS split_adj_factor;
ALTER TABLE price_bars_daily ALTER COLUMN adj_close TYPE NUMERIC(18, 6);
