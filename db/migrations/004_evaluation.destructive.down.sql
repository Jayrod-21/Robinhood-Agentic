-- 004_evaluation (down) — drop the learning-loop tables.
--
-- Data loss: every agent registry entry, debate, proposal, judgment, paper portfolio, daily return,
-- computed metric, and knowledge-base lesson. The counterfactual track records are NOT recoverable
-- by replay — they depend on marks taken at the time, against prices as they then stood.
--
-- Dropped in reverse dependency order. knowledge_base_entries first (it references almost
-- everything), then metrics, then returns, then portfolios, then the debate structures, then agents.

DROP TRIGGER IF EXISTS trg_kb_updated_at ON knowledge_base_entries;

DROP TABLE IF EXISTS knowledge_base_entries;
DROP TABLE IF EXISTS evaluation_runs;
DROP TABLE IF EXISTS portfolio_returns_daily;
DROP TABLE IF EXISTS paper_portfolios;
DROP TABLE IF EXISTS agent_proposal_positions;
DROP TABLE IF EXISTS agent_proposals;
DROP TABLE IF EXISTS judgments;
DROP TABLE IF EXISTS debates;
DROP TABLE IF EXISTS agents;
