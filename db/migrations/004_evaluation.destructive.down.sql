-- 004_evaluation (down) — drop the learning-loop tables.
--
-- Data loss: every agent registry entry, debate, proposal, judgment, paper portfolio, holding,
-- daily return, computed metric, guardrail event, risk-free rate observation, calendar row, and
-- knowledge-base lesson. The counterfactual track records are NOT recoverable by replay — they
-- depend on marks taken at the time, against prices as they then stood.
--
-- Dropped in reverse dependency order: knowledge_base_entries and guardrail_events first (they
-- reference almost everything), then metrics, then returns, then holdings, then judgments (which
-- reference portfolios AND proposals), then portfolios, then the debate structures, then agents,
-- then the standalone reference tables. 004's trigger functions go after the tables that carry
-- their triggers; 001's set_updated_at() is NOT ours to drop.

DROP TABLE IF EXISTS knowledge_base_entries;
DROP TABLE IF EXISTS guardrail_events;
DROP TABLE IF EXISTS evaluation_runs;
DROP TABLE IF EXISTS portfolio_returns_daily;
DROP TABLE IF EXISTS paper_portfolio_positions;
DROP TABLE IF EXISTS judgments;
DROP TABLE IF EXISTS paper_portfolios;
DROP TABLE IF EXISTS agent_proposal_positions;
DROP TABLE IF EXISTS agent_proposals;
DROP TABLE IF EXISTS debates;
DROP TABLE IF EXISTS agents;
DROP TABLE IF EXISTS risk_free_rates;
DROP TABLE IF EXISTS market_calendar;

DROP FUNCTION IF EXISTS enforce_proposal_weight_sum();
DROP FUNCTION IF EXISTS enforce_paper_portfolio_inception();
DROP FUNCTION IF EXISTS enforce_prd_mark_after_inception();
DROP FUNCTION IF EXISTS enforce_eval_run_n_observations();
