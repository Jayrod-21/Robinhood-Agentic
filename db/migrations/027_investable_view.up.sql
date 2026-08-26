-- 027 — one definition of "investable", reachable by every process.
--
-- THE PROBLEM THIS SOLVES
--   #41 put the classifier in db/instrument_class.py and said is_investable() is "the single
--   definition of what a universe may draw from". That is true for anything that can import it.
--   The Testing Lab cannot: its image ships lab/ and src/ and deliberately not db/, so the only way
--   it could apply the filter was to hardcode ('stock','etf','share_class') in a query — a second
--   definition, in a different language, in a different container.
--
--   Two definitions of a universe filter is how one of them drifts. #135 already showed what that
--   costs: 025's index predicate said ('stock','etf'), the classifier said the same, and both were
--   wrong about share classes — so BRK.B, a held position, silently left the universe.
--
--   A view is the fix that needs no import. Both the backend and the Lab already hold a connection
--   to this database; neither needs to hold a copy of the rule.
--
-- WHAT IT IS NOT
--   A judgement about quality, liquidity, or whether a name is worth trading. It answers exactly
--   one question — is this instrument a company or a fund, as opposed to a warrant, a unit, a
--   right, or something the data provider has never heard of. `src/universe.py` remains the
--   curated watchlist the daily scan actually screens; this is the far larger set that a backtest
--   or a training run may legitimately draw from.

CREATE OR REPLACE VIEW investable_securities AS
    SELECT id, symbol, name, security_type, exchange, sector, industry, delisted_at
      FROM securities
     -- Must stay in step with db/instrument_class.py::INVESTABLE. db/tests/test_investable_view.py
     -- asserts the two agree, so a change to either without the other is a red test rather than a
     -- universe that quietly means two different things.
     WHERE security_type IN ('stock', 'etf', 'share_class');

COMMENT ON VIEW investable_securities IS
    'Securities a screen, a backtest or a Testing Lab training run may draw from: companies and '
    'funds, excluding warrants, units, rights and instruments the provider does not carry. '
    'Mirrors db/instrument_class.py::INVESTABLE, pinned by db/tests/test_investable_view.py. '
    'NULL security_type is excluded — an unclassified row is not known to be investable. See #41.';

GRANT SELECT ON investable_securities TO rh_app;
