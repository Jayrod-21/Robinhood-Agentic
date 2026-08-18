-- 016_fundamentals_full — the rest of the Bloomberg pull, plus a decomposable Piotroski.
--
-- 003 modelled the Sprinkle Sauce screen inputs. The owner's actual Bloomberg sheet is wider: market
-- multiples (EV/EBITDA, P/B, P/S), balance-sheet absolutes (capex, net debt, shares), analyst
-- consensus, and ownership. Those had nowhere to go, so they were either dropped or would have been
-- buried in `extra` where nothing can query them.
--
-- WHAT IS DERIVED AND WHAT IS FETCHED
--     Several of these are computed by us rather than reported by the vendor — EPS growth, R&D as a
--     share of revenue, price/tangible-book, equity/assets. A derived number sitting in a column
--     beside vendor-supplied ones is indistinguishable from one, which matters the day a figure
--     looks wrong and someone has to work out whether to doubt the source or the arithmetic.
--     `derived_fields` records which keys in a row we computed.
--
-- PIOTROSKI IS STORED WITH ITS WORKING
--     A 0-9 score with no components is unfalsifiable: it cannot be checked against Bloomberg, and a
--     mapping error surfaces as a number that is merely wrong rather than one that is visibly wrong.
--     `piotroski_signals` holds all nine booleans with the inputs behind them.
--
--     `piotroski_variant` names WHICH definition produced the score. This project uses Cary's, which
--     differs from the canonical F-Score in two of nine signals: it tests whether net income and
--     operating cash flow IMPROVED year over year, where the textbook tests whether they are
--     POSITIVE. A company with negative but improving earnings scores 2 under Cary's and 0 under
--     Piotroski (1998). Both are defensible; silently mixing them in one column is not, and the
--     Bloomberg validation set was generated with Cary's.

ALTER TABLE fundamentals_snapshots
    -- Market multiples
    ADD COLUMN dividend_yield              numeric(18, 6),
    ADD COLUMN ev_to_ebitda                numeric(18, 6),
    ADD COLUMN price_to_book               numeric(18, 6),
    ADD COLUMN price_to_sales              numeric(18, 6),
    ADD COLUMN price_to_tangible_book      numeric(18, 6),
    ADD COLUMN beta                        numeric(18, 6),
    -- Range and liquidity
    ADD COLUMN week_52_high                numeric(18, 6),
    ADD COLUMN week_52_low                 numeric(18, 6),
    ADD COLUMN avg_volume_30d              bigint,
    -- Absolutes
    ADD COLUMN revenue_ttm                 numeric(24, 2),
    ADD COLUMN ebitda_ttm                  numeric(24, 2),
    ADD COLUMN capital_expenditure         numeric(24, 2),
    ADD COLUMN net_debt                    numeric(24, 2),
    ADD COLUMN shares_outstanding          numeric(24, 4),
    ADD COLUMN tangible_book_value_per_share numeric(18, 6),
    -- Growth and intensity
    ADD COLUMN eps_growth_yoy              numeric(18, 6),
    ADD COLUMN rd_to_revenue               numeric(18, 6),
    ADD COLUMN equity_to_assets            numeric(18, 6),
    -- Analyst consensus
    ADD COLUMN analyst_target_price        numeric(18, 6),
    ADD COLUMN analyst_recommendation      text,
    -- Provenance of our own arithmetic
    ADD COLUMN derived_fields              jsonb,
    ADD COLUMN piotroski_variant           text,
    ADD COLUMN piotroski_signals           jsonb;

ALTER TABLE fundamentals_snapshots
    ADD CONSTRAINT ck_fundamentals_derived_obj
        CHECK (derived_fields IS NULL OR jsonb_typeof(derived_fields) = 'object'),
    ADD CONSTRAINT ck_fundamentals_piotroski_signals_obj
        CHECK (piotroski_signals IS NULL OR jsonb_typeof(piotroski_signals) = 'object'),
    -- A score without a named definition is not interpretable, so the two travel together.
    ADD CONSTRAINT ck_fundamentals_piotroski_variant
        CHECK (piotroski_f_score IS NULL OR piotroski_variant IS NOT NULL);

COMMENT ON COLUMN fundamentals_snapshots.derived_fields IS
    'Keys in this row we COMPUTED rather than received from the vendor, with the formula used. A '
    'derived number beside a fetched one is indistinguishable from it until someone needs to know '
    'whether to doubt the source or the arithmetic.';
COMMENT ON COLUMN fundamentals_snapshots.piotroski_variant IS
    'Which F-Score definition produced piotroski_f_score. "cary" tests YoY improvement in net income '
    'and operating cash flow; "piotroski1998" tests their positivity. They disagree by up to two '
    'points on a distressed name, so the scores must never be compared without this column.';
COMMENT ON COLUMN fundamentals_snapshots.piotroski_signals IS
    'All nine signals with the inputs behind them. A score with no working cannot be validated '
    'against Bloomberg, and a mapping error would surface as a number that is merely wrong rather '
    'than visibly wrong.';

CREATE INDEX ix_fundamentals_piotroski ON fundamentals_snapshots (piotroski_f_score DESC)
    WHERE piotroski_f_score IS NOT NULL;
