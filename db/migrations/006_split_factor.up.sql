-- 006_split_factor — store the cumulative split factor, and widen adj_close.
--
-- WHY
--   005 populates adj_close as close ÷ Π(split_ratio after t). For forward splits that shrinks
--   historical prices, which is fine. For REVERSE splits the ratio is < 1, so the division
--   MULTIPLIES them — and serial reverse-splitters compound it hard. Measured in this archive:
--
--       WHLR  16 splits   NUWE  8 splits, cumulative factor ≈ 1e-10, implied adj price ≈ $21 trillion
--       UVXY  13 splits   ADTX  7 splits, implied ≈ $28 trillion
--
--   Those are dying penny stocks and leveraged volatility ETFs, and the arithmetic is CORRECT: one
--   share today corresponds to a vanishing fraction of a share five years ago. But NUMERIC(18,6)
--   overflows at 1e12, so the whole adjustment pass aborted on them.
--
-- THE FIX, AND WHY IT IS NOT JUST A WIDER COLUMN
--   Widening alone postpones the problem — 30 years of FMP history will contain worse. The stable
--   representation is the FACTOR, not the adjusted level:
--
--       return(t) = adj_close(t) / adj_close(t-1)
--                 = (close(t)/factor(t)) / (close(t-1)/factor(t-1))
--
--   The astronomically large intermediate cancels. A consumer computing returns never needs the
--   adjusted price LEVEL at all — only the factor — so the factor is what must always be present
--   and exact, while adj_close is a convenience that may legitimately be unrepresentable.
--
--   So: `split_adj_factor` is added and is always populated. `adj_close` is widened to hold every
--   realistic case, and is left NULL for the pathological ones rather than the load failing. A NULL
--   there now means "this level is not representable — use the factor", which is why the marking
--   job must treat a NULL adj_close as an error rather than a zero.
--
-- ALTER COLUMN TYPE rewrites the table (~12.8M rows). That is a few minutes and is why this is its
-- own migration rather than a patch to 002.

ALTER TABLE price_bars_daily
    ALTER COLUMN adj_close TYPE NUMERIC(30, 10);

-- Cumulative product of every split taking effect AFTER this bar's date. 1 when none.
-- NUMERIC(30,12) holds 1e-12 through 1e18, covering every ratio observed and a wide margin.
ALTER TABLE price_bars_daily
    ADD COLUMN IF NOT EXISTS split_adj_factor NUMERIC(30, 12);

-- Strictly positive: a factor is a product of ratios, each of which the actions table already
-- constrains to > 0. Zero or negative would mean a corrupt product, and dividing by it would either
-- error or silently flip the sign of every historical price.
ALTER TABLE price_bars_daily
    ADD CONSTRAINT ck_price_bars_daily_factor CHECK (split_adj_factor IS NULL OR split_adj_factor > 0);

COMMENT ON COLUMN price_bars_daily.split_adj_factor IS
    'Cumulative product of split ratios taking effect AFTER trade_date; 1 when none. THE stable '
    'representation — returns are (close(t)/f(t)) ÷ (close(t-1)/f(t-1)), where the large '
    'intermediate cancels. Always populated once the adjustment has run.';

COMMENT ON COLUMN price_bars_daily.adj_close IS
    'SPLIT-adjusted close = close ÷ split_adj_factor, widened to NUMERIC(30,10). NOT '
    'dividend-adjusted: dividends become cash at marking, so adjusting the price too would '
    'double-count. NULL means the level is not representable (serial reverse-splitters reach ~1e13) '
    '— use split_adj_factor instead. A consumer that needs a level MUST treat NULL as an error, '
    'never as zero.';
