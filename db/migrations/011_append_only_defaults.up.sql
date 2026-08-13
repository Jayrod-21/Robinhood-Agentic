-- 011_append_only_defaults — close the mechanism that re-opens append-only (issue #34).
--
-- 001 granted SELECT, INSERT, UPDATE, DELETE on ALL FUTURE tables to rh_app via
-- ALTER DEFAULT PRIVILEGES. 004 then REVOKEd UPDATE/DELETE per append-only table — fixing the
-- instances while leaving the mechanism that produces them: every table a future migration creates
-- is born fully mutable, and if its author forgets the per-table REVOKE the failure is invisible
-- (the comment promises append-only, the grant says otherwise, nothing complains). That is the
-- defect the whole 004 review cycle existed to close, one level up.
--
-- FIX 1 — fail closed at the mechanism: the default grant for future tables narrows to
-- SELECT, INSERT. A future MUTABLE table must now say so explicitly in its own migration:
--
--     GRANT UPDATE, DELETE ON my_mutable_table TO rh_app;
--
-- Forgetting now yields a too-tight table whose first UPDATE fails loudly with a permission
-- error — instead of a silently mutable history table. Matches the project's stated posture
-- (fail closed, fail loud) and the trading-guardrail rule: the block is observable and the
-- override is a one-line grant, never a silent hole.
--
-- Existing tables are untouched: default privileges apply only at CREATE time, so every grant
-- already in the catalog (including full DML on securities, price bars, corporate_actions,
-- price_adjustment_state, price_gap_audit — verified live 2026-08-13: none of those claims
-- append-only) stays exactly as it was. Future price_bars_minute_* partitions will be born
-- SELECT+INSERT for rh_app; DML routed through the partitioned PARENT checks the parent's ACL,
-- so app-visible behaviour is unchanged, and loaders run as the migration role anyway.
--
-- FIX 2 — the declaration becomes machine-readable: a table that is append-only BY GRANTS carries
-- the exact marker 'APPEND-ONLY (enforced by grants)' in its table comment. db/tests enumerates
-- tables by that marker from the catalog and asserts rh_app holds no UPDATE/DELETE on each — the
-- gate no longer depends on a hardcoded table list that a future migration's author must remember
-- to extend. knowledge_base_entries already carries the marker verbatim (004) and is not touched
-- here. data_sources ("append-only by policy": FK RESTRICT, but row_count/notes are legitimately
-- corrected) and fundamentals_snapshots ("append-only observations": loader-corrected) are
-- deliberately NOT marked — their append-only-ness is policy, not grants.

-- ── FIX 1: narrow the default grant for future tables ────────────────────────────────────────
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE UPDATE, DELETE ON TABLES FROM rh_app;

-- ── FIX 2: canonical append-only markers on the grant-enforced tables ────────────────────────
-- Prose is carried over from 004; only the declaration wording is normalised to the marker.

COMMENT ON TABLE evaluation_runs IS
    'Metric snapshots for a (portfolio, window): recomputing with different weights or newer code '
    'writes a NEW row. Each row records the rf/MAR/annualisation that produced its ratios, so any '
    'two rows are comparable — or visibly not. APPEND-ONLY (enforced by grants).';

COMMENT ON TABLE portfolio_returns_daily IS
    'Daily portfolio marks — the observation series every Sharpe and Sortino is computed from, '
    'with priced_as_of pinning each mark''s provenance inside the lookahead bound. Scored '
    'history: corrections are the migration role''s job, never the app''s. '
    'APPEND-ONLY (enforced by grants).';

COMMENT ON TABLE agent_proposals IS
    'What each persona proposed in each debate, win or lose — the seed of every counterfactual '
    'track record. A failed debate''s proposals cascade away with the debate. '
    'APPEND-ONLY (enforced by grants).';

COMMENT ON TABLE agent_proposal_positions IS
    'Target weights as a percent of ACCOUNT VALUE (cash included) — the same denominator the '
    'charter''s per-name limit uses. Per-proposal sum is capped at 100 by trg_app_weight_sum. '
    'APPEND-ONLY (enforced by grants).';

COMMENT ON TABLE judgments IS
    'Which judge ruled what, why, which proposal it backed, and the portfolio the ruling produced. '
    'judgment → resulting_portfolio → evaluation_runs is the §3.2 self-review join. APPEND-ONLY '
    '(enforced by grants), except resulting_portfolio_id — a column-level UPDATE grant, set once '
    'the book exists.';

COMMENT ON TABLE guardrail_events IS
    'Durable record of every guardrail trip: rule, threshold, observed, action, override. The '
    'data source for EVALUATION_FRAMEWORK §5''s guardrail_breach_penalty, and the query that makes '
    'a mis-set limit visible in seconds. APPEND-ONLY (enforced by grants).';

COMMENT ON TABLE risk_free_rates IS
    'Point-in-time risk-free rate observations (fraction, annual). known_at is part of the '
    'identity — a revision is a new row. evaluation_runs.risk_free_annual is sourced from here. '
    'APPEND-ONLY (enforced by grants).';
