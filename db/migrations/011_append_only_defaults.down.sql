-- 011_append_only_defaults (down) — restore 001's broad default grant and 004's comment texts.
--
-- No table data is touched in either direction, hence no destructive marker (ADR-002: the
-- filename declares destructiveness; the runner's blanket rollback gate still applies). The
-- GRANT below re-opens the future-table default to SELECT, INSERT, UPDATE, DELETE — exactly the
-- state 001 left behind — and the comments revert verbatim to the 004 texts
-- (portfolio_returns_daily had none).

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT UPDATE, DELETE ON TABLES TO rh_app;

COMMENT ON TABLE evaluation_runs IS
    'Append-only metric snapshots (ENFORCED: rh_app holds no UPDATE/DELETE). Recomputing with '
    'different weights or newer code writes a NEW row. Each row records the rf/MAR/annualisation '
    'that produced its ratios, so any two rows are comparable — or visibly not.';

COMMENT ON TABLE portfolio_returns_daily IS NULL;

COMMENT ON TABLE agent_proposals IS
    'What each persona proposed in each debate, win or lose — the seed of every counterfactual '
    'track record. Append-only for the runtime role; a failed debate''s proposals cascade away '
    'with the debate.';

COMMENT ON TABLE agent_proposal_positions IS
    'Target weights as a percent of ACCOUNT VALUE (cash included) — the same denominator the '
    'charter''s per-name limit uses. Per-proposal sum is capped at 100 by trg_app_weight_sum.';

COMMENT ON TABLE judgments IS
    'Which judge ruled what, why, which proposal it backed, and the portfolio the ruling produced. '
    'judgment → resulting_portfolio → evaluation_runs is the §3.2 self-review join. Append-only '
    'for the runtime role except resulting_portfolio_id, which is set once the book exists.';

COMMENT ON TABLE guardrail_events IS
    'Durable record of every guardrail trip: rule, threshold, observed, action, override. The '
    'data source for EVALUATION_FRAMEWORK §5''s guardrail_breach_penalty, and the query that makes '
    'a mis-set limit visible in seconds. Append-only for the runtime role.';

COMMENT ON TABLE risk_free_rates IS
    'Point-in-time risk-free rate observations (fraction, annual). known_at is part of the '
    'identity — a revision is a new row. evaluation_runs.risk_free_annual is sourced from here.';
