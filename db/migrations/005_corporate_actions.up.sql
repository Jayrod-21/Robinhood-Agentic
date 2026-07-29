-- 005_corporate_actions — splits and cash dividends, and the adjusted close derived from them.
--
-- WHY THIS EXISTS
--   `price_bars_daily.adj_close` has been NULL since 002 while its comment promised returns would
--   use it. Verified in our own loaded data: NVDA closes 751.19 on 2021-07-19 and 186.06 on
--   2021-07-20, a naive −75.23% day. That is a 4-for-1 split, not a crash. NVDA splits AGAIN 10:1
--   in June 2024, so the cumulative distortion across our window is 40×.
--
--   Every downstream number reads this: the marking job, every Sharpe and Sortino, every
--   counterfactual track record, every backtest. Unadjusted, they are not merely imprecise — they
--   are wrong in a direction that looks like a catastrophic loss or a spectacular gain.
--
-- SPLIT-ADJUSTED, NOT TOTAL-RETURN ADJUSTED — and the distinction matters
--   `adj_close` here is adjusted for SPLITS ONLY. 002's comment called it "split/dividend adjusted",
--   which is the conventional phrase but the wrong behaviour for this system.
--
--   A dividend-adjusted price series bakes the dividend into the price so that a price-only return
--   equals the total return. But this system marks portfolios as `Σ shares × adj_close + cash`, and
--   the marking job credits dividends to CASH. Using dividend-adjusted prices as well would count
--   every dividend twice.
--
--   So: splits change the share count and must adjust the price series to keep it continuous.
--   Dividends do not change the share count; they are recorded here and become cash at the marking
--   step. 002's comment is corrected accordingly.
--
-- THE ADJUSTMENT
--   adj_close(t) = close(t) ÷ Π(split_ratio for every split with ex_date > t)
--
--   Prices BEFORE a split are divided by the ratio so the series is continuous across it. Checked
--   against the NVDA case: 751.19 ÷ 4 = 187.80 against a post-split 186.06, which turns a −75.23%
--   artefact into a −0.93% real move.
--
-- migrate: filename carries no destructive marker — this migration only creates.

CREATE TABLE IF NOT EXISTS corporate_actions (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    security_id  BIGINT      NOT NULL,

    -- 'split'         — includes reverse splits (ratio < 1) and stock dividends expressed as a ratio
    -- 'cash_dividend' — becomes cash at marking time; never adjusts the price series
    action_type  TEXT        NOT NULL,

    -- The date the PRICE reflects the action — the first session trading on the new basis. Not the
    -- declaration, record, or payment date. Adjustment keys off this and nothing else.
    ex_date      DATE        NOT NULL,

    -- Shares held AFTER per share held BEFORE. A 4-for-1 is 4.0; a 1-for-10 reverse is 0.1.
    split_ratio  NUMERIC(18, 8),

    -- Cash per share, in the security's currency.
    cash_amount  NUMERIC(18, 8),

    -- When this action became publicly known, when the provider supplies it. NULL means unknown —
    -- and such rows must be excluded from any point-in-time claim rather than assumed announced on
    -- the ex-date, the same rule 003 applies to `known_at`.
    announced_at TIMESTAMPTZ,

    source_id    BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_corporate_actions_security FOREIGN KEY (security_id)
        REFERENCES securities (id) ON DELETE RESTRICT,
    CONSTRAINT fk_corporate_actions_source FOREIGN KEY (source_id)
        REFERENCES data_sources (id) ON DELETE RESTRICT,

    CONSTRAINT ck_corporate_actions_type CHECK (action_type IN ('split', 'cash_dividend')),

    -- Each type carries exactly its own payload and nothing else. A split with a cash_amount, or a
    -- dividend with a ratio, is a loader bug — and one that would silently corrupt the adjustment.
    CONSTRAINT ck_corporate_actions_payload CHECK (
        (action_type = 'split'
            AND split_ratio IS NOT NULL AND split_ratio > 0 AND cash_amount IS NULL)
        OR
        (action_type = 'cash_dividend'
            AND cash_amount IS NOT NULL AND cash_amount > 0 AND split_ratio IS NULL)
    ),

    -- A ratio of exactly 1 adjusts nothing and is almost certainly a parsing artefact. Rejecting it
    -- keeps a no-op row from being mistaken for a recorded split.
    CONSTRAINT ck_corporate_actions_ratio_meaningful CHECK (
        action_type <> 'split' OR split_ratio <> 1
    ),

    CONSTRAINT ck_corporate_actions_announced CHECK (
        announced_at IS NULL OR announced_at <= (ex_date + 1)::timestamptz
    )
);

-- One action of a given type per security per ex-date. Two splits on one day is not a real
-- corporate event; it is a double-load, and it would square the adjustment factor.
CREATE UNIQUE INDEX IF NOT EXISTS uq_corporate_actions
    ON corporate_actions (security_id, action_type, ex_date);

-- The adjustment query's access pattern: "every split for this security after this date".
CREATE INDEX IF NOT EXISTS ix_corporate_actions_split_lookup
    ON corporate_actions (security_id, ex_date DESC)
    WHERE action_type = 'split';

CREATE INDEX IF NOT EXISTS ix_corporate_actions_ex_date ON corporate_actions (ex_date DESC);

DROP TRIGGER IF EXISTS trg_corporate_actions_updated_at ON corporate_actions;
CREATE TRIGGER trg_corporate_actions_updated_at
    BEFORE UPDATE ON corporate_actions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE corporate_actions IS
    'Splits and cash dividends. Splits adjust the price series (they change the share count); '
    'dividends do not — they become cash at marking time. Adjusting prices for dividends AND '
    'crediting the cash would double-count.';
COMMENT ON COLUMN corporate_actions.ex_date IS
    'First session trading on the new basis. Not declaration, record, or payment date.';
COMMENT ON COLUMN corporate_actions.split_ratio IS
    'Shares AFTER per share BEFORE. 4-for-1 = 4.0; 1-for-10 reverse = 0.1.';

-- ── the adjustment ───────────────────────────────────────────────────────────────────────────
-- Cumulative future split factor for one security as of one date. Kept as a function so the
-- adjustment rule lives in exactly one place: the backfill, any incremental update, and any test
-- all call this rather than reimplementing the product.
CREATE OR REPLACE FUNCTION split_factor_after(p_security_id BIGINT, p_date DATE)
RETURNS NUMERIC
LANGUAGE sql STABLE AS $$
    -- COALESCE to 1 when there are no later splits: the price then needs no adjustment.
    --
    -- ROUNDed because exp(sum(ln(x))) goes through double precision and does not round-trip: a
    -- single 4-for-1 returns 3.9999999999999999, and the error compounds with each split (NVDA has
    -- two in this archive). 12 decimal places is exact for any real split ratio — they are simple
    -- rationals like 4, 1.5, or 0.1 — while removing the artefact. Postgres has no aggregate
    -- product, which is why the logarithm identity is used at all.
    SELECT ROUND(COALESCE(
        (SELECT exp(sum(ln(split_ratio)))
         FROM corporate_actions
         WHERE security_id = p_security_id
           AND action_type = 'split'
           AND ex_date > p_date),
        1
    ), 12);
$$;

COMMENT ON FUNCTION split_factor_after(BIGINT, DATE) IS
    'Product of every split ratio taking effect AFTER p_date. Divide a raw close by this to get the '
    'split-adjusted close. Returns 1 when no later split exists. Computed as exp(sum(ln)) because '
    'Postgres has no aggregate product; every ratio is > 0 by CHECK, so ln() is always defined.';

-- Correct 002's description of adj_close, which called it "split/dividend adjusted" — the
-- conventional phrase, but the wrong behaviour for this system (see the header).
--
-- Issued HERE rather than by editing 002, because 002 is already applied and its body is
-- checksummed: editing it would raise ChecksumMismatch, which is the guard working as designed.
-- A COMMENT ON is idempotent and re-issuable, so the correction lands in the migration that
-- actually changes the meaning.
COMMENT ON COLUMN price_bars_daily.adj_close IS
    'SPLIT-adjusted close, populated from corporate_actions (005). NOT dividend-adjusted: the '
    'marking job credits dividends to cash, so adjusting the price too would double-count. Returns '
    'use this series; fills happened at the raw close.';
