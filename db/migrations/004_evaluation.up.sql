-- 004_evaluation — agents, debates, proposals, judgments, paper portfolios, returns, metrics,
-- and the knowledge base. The tables the learning loop reads and writes.
--
-- Specified by docs/EVALUATION_FRAMEWORK.md §4. That document explains WHY; this one explains the
-- modelling. Read it first if the shape here looks surprising.
--
-- The central idea: a decision is scored by its RISK-ADJUSTED outcome, not by whether it made money.
-- An objective of "20-40% monthly return" with no risk term makes maximum concentration the rational
-- play, because it is the only thing that plausibly reaches the target. Sharpe and Sortino are the
-- correction, and both are carried — they disagree informatively, since Sharpe penalises upside
-- volatility that an aggressive book actively wants.
--
-- Two constraints the framework requires at the schema level, both enforced below:
--   1. `n_observations` is NOT NULL on evaluation_runs. A ratio without its sample size invites
--      somebody to read six days of noise as skill.
--   2. Every row records the as-of timestamp of its inputs, so a leakage audit is a query rather
--      than an excavation.
--
-- The runner owns the transaction: no BEGIN/COMMIT here.

-- ── agents ───────────────────────────────────────────────────────────────────────────────────
-- The persona registry. VERSIONED, because a prompt change makes it a different agent for scoring
-- purposes — comparing a persona's record across a prompt rewrite would silently blend two
-- different reasoners into one track record.
CREATE TABLE IF NOT EXISTS agents (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Stable identity across versions: 'bull', 'bear', 'wasden', 'blind', 'judge_risk', …
    agent_key    TEXT        NOT NULL,
    version      INTEGER     NOT NULL,

    -- 'persona'  — debates a position
    -- 'judge'    — rules on a debate (§3.2: reviews the outcome of its OWN prior judgments)
    -- 'blind'    — the control. No persona, no exposure to the return-target prompt. Exists to
    --              answer "does the debate machinery beat a plain agent?" — a question only
    --              answerable if the control is scored from the start.
    -- 'real'     — the live Robinhood account, so it sits on the same leaderboard as everything else
    kind         TEXT        NOT NULL,

    display_name TEXT,
    model        TEXT,
    -- SHA-256 of the exact prompt this version ran. Two agents claiming the same version with
    -- different prompts is a bug worth catching, and the hash makes it detectable.
    prompt_sha256 TEXT,
    prompt_text  TEXT,
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set when superseded. Never DELETE an agent: its historical proposals and scores must remain
    -- attributable, exactly as delisted securities are retained in 001.
    retired_at   TIMESTAMPTZ,

    CONSTRAINT ck_agents_key      CHECK (agent_key ~ '^[a-z][a-z0-9_]{1,48}$'),
    CONSTRAINT ck_agents_kind     CHECK (kind IN ('persona', 'judge', 'blind', 'real')),
    CONSTRAINT ck_agents_version  CHECK (version >= 1),
    CONSTRAINT ck_agents_sha      CHECK (prompt_sha256 IS NULL OR prompt_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_agents_retired  CHECK (retired_at IS NULL OR retired_at >= created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_key_version ON agents (agent_key, version);
-- At most one live version per key. Two active versions of 'bull' would silently split its record.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_active ON agents (agent_key) WHERE retired_at IS NULL;
-- The blind control is a singleton by design; more than one would make "the control" ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_one_blind ON agents ((kind)) WHERE kind = 'blind' AND retired_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_one_real ON agents ((kind)) WHERE kind = 'real' AND retired_at IS NULL;

COMMENT ON TABLE agents IS
    'Persona registry, versioned — a prompt change is a new version, because a track record must not '
    'blend two different reasoners. Agents are retired, never deleted.';
COMMENT ON COLUMN agents.kind IS
    'persona | judge | blind (the unbiased control) | real (the live account, scored on the same '
    'leaderboard).';

-- ── debates ──────────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS debates (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- 'ticker' — one name; 'slate' — the whole book's allocation
    scope        TEXT        NOT NULL,
    security_id  BIGINT,
    question     TEXT,

    -- THE LEAKAGE ANCHOR. Every fact fed to this debate must have been knowable at this instant:
    -- fundamentals filtered on known_at <= context_as_of, bars with ts <= context_as_of. A backtest
    -- or replay that ignores this is reporting an edge it does not have. NOT NULL on purpose —
    -- "we forgot to record the cutoff" and "there was no cutoff" must not look the same.
    context_as_of TIMESTAMPTZ NOT NULL,

    status       TEXT        NOT NULL DEFAULT 'running',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_debates_security FOREIGN KEY (security_id)
        REFERENCES securities (id) ON DELETE RESTRICT,
    CONSTRAINT ck_debates_scope    CHECK (scope IN ('ticker', 'slate')),
    -- A ticker debate without a security is meaningless; a slate debate with one is a modelling error.
    CONSTRAINT ck_debates_scope_security CHECK (
        (scope = 'ticker' AND security_id IS NOT NULL) OR
        (scope = 'slate'  AND security_id IS NULL)
    ),
    CONSTRAINT ck_debates_status   CHECK (status IN ('running', 'complete', 'failed', 'abandoned')),
    CONSTRAINT ck_debates_complete CHECK (completed_at IS NULL OR completed_at >= started_at)
);

CREATE INDEX IF NOT EXISTS ix_debates_started ON debates (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_debates_security ON debates (security_id, started_at DESC)
    WHERE security_id IS NOT NULL;

COMMENT ON COLUMN debates.context_as_of IS
    'Point-in-time cutoff. Every input must have been knowable at this instant. NOT NULL so a missing '
    'cutoff cannot be mistaken for no cutoff.';

-- ── proposals ────────────────────────────────────────────────────────────────────────────────
-- What each persona argued for, WHETHER OR NOT IT WON. This is the seed of every counterfactual
-- track record (§3.3) and the reason a debate yields N observations instead of one: losing
-- proposals are scored too.
CREATE TABLE IF NOT EXISTS agent_proposals (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    debate_id    BIGINT      NOT NULL,
    agent_id     BIGINT      NOT NULL,
    stance       TEXT        NOT NULL,
    -- 0..1. Stored because a confident wrong call and a hedged wrong call are different failures,
    -- and calibration is only measurable if confidence was recorded at the time.
    confidence   NUMERIC(5, 4),
    rationale    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_agent_proposals_debate FOREIGN KEY (debate_id)
        REFERENCES debates (id) ON DELETE CASCADE,
    CONSTRAINT fk_agent_proposals_agent FOREIGN KEY (agent_id)
        REFERENCES agents (id) ON DELETE RESTRICT,
    CONSTRAINT ck_agent_proposals_stance CHECK (stance IN ('buy', 'sell', 'hold', 'abstain')),
    CONSTRAINT ck_agent_proposals_conf   CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_proposals ON agent_proposals (debate_id, agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_proposals_agent ON agent_proposals (agent_id, created_at DESC);

-- Target weights per proposal. A separate table rather than JSONB because these are joined against
-- securities and aggregated constantly — the queries that build a counterfactual portfolio are
-- relational, not document lookups.
CREATE TABLE IF NOT EXISTS agent_proposal_positions (
    proposal_id  BIGINT      NOT NULL,
    security_id  BIGINT      NOT NULL,
    -- Percent of ACCOUNT VALUE (cash included), matching the charter's "~25% of account value per
    -- name". Note the dashboard currently shows share-of-equity — a different denominator, and the
    -- source of issue #21. Percentages here are always account-value.
    target_weight_pct NUMERIC(7, 4) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_app_proposal FOREIGN KEY (proposal_id)
        REFERENCES agent_proposals (id) ON DELETE CASCADE,
    CONSTRAINT fk_app_security FOREIGN KEY (security_id)
        REFERENCES securities (id) ON DELETE RESTRICT,
    -- Shorting is out of scope for a cash account; a negative target is a bug, not a position.
    CONSTRAINT ck_app_weight CHECK (target_weight_pct >= 0 AND target_weight_pct <= 100),
    PRIMARY KEY (proposal_id, security_id)
);

COMMENT ON TABLE agent_proposal_positions IS
    'Target weights as a percent of ACCOUNT VALUE (cash included) — the same denominator the '
    'charter''s per-name limit uses.';

-- ── judgments ────────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS judgments (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    debate_id      BIGINT      NOT NULL,
    judge_agent_id BIGINT      NOT NULL,
    decision       TEXT        NOT NULL,
    confidence     NUMERIC(5, 4),
    rationale      TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_judgments_debate FOREIGN KEY (debate_id)
        REFERENCES debates (id) ON DELETE CASCADE,
    CONSTRAINT fk_judgments_agent FOREIGN KEY (judge_agent_id)
        REFERENCES agents (id) ON DELETE RESTRICT,
    CONSTRAINT ck_judgments_decision CHECK (decision IN ('buy', 'sell', 'hold', 'escalate')),
    CONSTRAINT ck_judgments_conf     CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_judgments ON judgments (debate_id, judge_agent_id);
-- §3.2: "show this judge the realized outcome of its own prior judgments." That is this index.
CREATE INDEX IF NOT EXISTS ix_judgments_agent_history ON judgments (judge_agent_id, created_at DESC);

COMMENT ON INDEX ix_judgments_agent_history IS
    'Serves EVALUATION_FRAMEWORK §3.2 — a judge reviewing the realized scores of its own prior rulings.';

-- ── paper portfolios ─────────────────────────────────────────────────────────────────────────
-- One per persona per debate (the counterfactual), plus the real account and the blind agent.
-- Marking these daily against real prices is what turns "the bear persona seems good" into a number.
CREATE TABLE IF NOT EXISTS paper_portfolios (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          TEXT        NOT NULL,
    agent_id      BIGINT,
    debate_id     BIGINT,
    proposal_id   BIGINT,
    inception_date DATE       NOT NULL,
    -- Every portfolio starts at the same notional so returns are comparable across agents.
    base_value    NUMERIC(18, 2) NOT NULL DEFAULT 100000.00,
    closed_at     TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_paper_portfolios_agent FOREIGN KEY (agent_id)
        REFERENCES agents (id) ON DELETE RESTRICT,
    CONSTRAINT fk_paper_portfolios_debate FOREIGN KEY (debate_id)
        REFERENCES debates (id) ON DELETE CASCADE,
    CONSTRAINT fk_paper_portfolios_proposal FOREIGN KEY (proposal_id)
        REFERENCES agent_proposals (id) ON DELETE CASCADE,
    CONSTRAINT ck_paper_portfolios_kind CHECK (kind IN ('counterfactual', 'real', 'blind')),
    CONSTRAINT ck_paper_portfolios_base CHECK (base_value > 0),
    -- A counterfactual is meaningless without the agent and proposal it came from; the real account
    -- has neither. Enforce the shape rather than trusting the writer.
    CONSTRAINT ck_paper_portfolios_shape CHECK (
        (kind = 'counterfactual' AND agent_id IS NOT NULL AND debate_id IS NOT NULL AND proposal_id IS NOT NULL) OR
        (kind = 'blind'          AND agent_id IS NOT NULL) OR
        (kind = 'real'           AND agent_id IS NOT NULL AND debate_id IS NULL AND proposal_id IS NULL)
    ),
    CONSTRAINT ck_paper_portfolios_closed CHECK (closed_at IS NULL OR closed_at::date >= inception_date)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_portfolios_counterfactual
    ON paper_portfolios (debate_id, agent_id) WHERE kind = 'counterfactual';
CREATE INDEX IF NOT EXISTS ix_paper_portfolios_agent ON paper_portfolios (agent_id, inception_date DESC);

-- ── daily returns ────────────────────────────────────────────────────────────────────────────
-- The observation table. Every Sharpe and Sortino in the system is computed from these rows, and
-- the framework is explicit that they are DAILY: on monthly returns you would need years before the
-- ratio meant anything.
CREATE TABLE IF NOT EXISTS portfolio_returns_daily (
    portfolio_id   BIGINT      NOT NULL,
    trade_date     DATE        NOT NULL,
    market_value   NUMERIC(18, 2) NOT NULL,
    -- Fractional, not percent: 0.0123 = +1.23%. Downstream maths is cleaner and unit confusion is
    -- the classic way a Sharpe comes out 100x wrong.
    daily_return   NUMERIC(12, 8),
    cumulative_return NUMERIC(12, 8),
    -- The mark's provenance: which price data produced this row. Lets a leakage audit confirm the
    -- mark used only prices available on the day, and nothing later.
    priced_as_of   TIMESTAMPTZ NOT NULL,
    source_id      BIGINT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_prd_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES paper_portfolios (id) ON DELETE CASCADE,
    CONSTRAINT fk_prd_source FOREIGN KEY (source_id)
        REFERENCES data_sources (id) ON DELETE RESTRICT,
    CONSTRAINT ck_prd_value CHECK (market_value >= 0),
    -- A daily return below -100% is impossible for a long-only cash book.
    CONSTRAINT ck_prd_return CHECK (daily_return IS NULL OR daily_return > -1),
    PRIMARY KEY (portfolio_id, trade_date)
);

CREATE INDEX IF NOT EXISTS ix_prd_date ON portfolio_returns_daily (trade_date);

COMMENT ON COLUMN portfolio_returns_daily.daily_return IS
    'Fractional (0.0123 = +1.23%), not percent. Mixing the two is how a Sharpe ends up 100x wrong.';

-- ── evaluation runs ──────────────────────────────────────────────────────────────────────────
-- Computed metrics for a (portfolio, window). APPEND-ONLY: recomputing with different reward
-- weights or a newer code version writes a NEW row. Overwriting would rewrite history and make any
-- comparison across time meaningless.
CREATE TABLE IF NOT EXISTS evaluation_runs (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_id   BIGINT      NOT NULL,
    window_start   DATE        NOT NULL,
    window_end     DATE        NOT NULL,

    -- THE CONSTRAINT THE FRAMEWORK INSISTS ON. A ratio without its sample size is not a usable
    -- number; nullable here would guarantee somebody eventually reads six days of noise as skill.
    n_observations INTEGER     NOT NULL,

    sharpe            NUMERIC(12, 6),
    sortino           NUMERIC(12, 6),
    max_drawdown      NUMERIC(12, 8),
    hit_rate          NUMERIC(5, 4),
    avg_win_loss      NUMERIC(12, 6),
    total_return      NUMERIC(12, 8),
    annualized_return NUMERIC(12, 8),
    volatility        NUMERIC(12, 8),
    information_ratio NUMERIC(12, 6),
    benchmark_symbol  TEXT,

    -- The composite the learning loop optimises, plus the weights that produced it. Storing the
    -- weights alongside is what makes a weight change a new observation rather than a silent
    -- rewrite of every past score.
    reward_total   NUMERIC(12, 6),
    reward_weights JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Reproducibility: which code computed this, and the latest input timestamp it saw.
    code_version   TEXT,
    inputs_as_of   TIMESTAMPTZ NOT NULL,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_evaluation_runs_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES paper_portfolios (id) ON DELETE CASCADE,
    CONSTRAINT ck_evaluation_runs_window CHECK (window_end >= window_start),
    CONSTRAINT ck_evaluation_runs_n      CHECK (n_observations >= 0),
    -- Standard deviation is undefined for n < 2, so a Sharpe or Sortino reported with fewer
    -- observations is arithmetically impossible, not merely unreliable. Reject it at the boundary.
    CONSTRAINT ck_evaluation_runs_sharpe_n  CHECK (sharpe  IS NULL OR n_observations >= 2),
    CONSTRAINT ck_evaluation_runs_sortino_n CHECK (sortino IS NULL OR n_observations >= 2),
    CONSTRAINT ck_evaluation_runs_hit_rate  CHECK (hit_rate IS NULL OR hit_rate BETWEEN 0 AND 1),
    -- Drawdown is expressed as a non-positive fraction (-0.23 = a 23% peak-to-trough fall).
    CONSTRAINT ck_evaluation_runs_dd        CHECK (max_drawdown IS NULL OR (max_drawdown <= 0 AND max_drawdown >= -1)),
    CONSTRAINT ck_evaluation_runs_weights   CHECK (jsonb_typeof(reward_weights) = 'object')
);

CREATE INDEX IF NOT EXISTS ix_evaluation_runs_portfolio
    ON evaluation_runs (portfolio_id, window_end DESC, computed_at DESC);
CREATE INDEX IF NOT EXISTS ix_evaluation_runs_computed ON evaluation_runs (computed_at DESC);

COMMENT ON TABLE evaluation_runs IS
    'Append-only metric snapshots. Recomputing with different weights or newer code writes a NEW '
    'row — overwriting would rewrite history and void every cross-time comparison.';
COMMENT ON COLUMN evaluation_runs.n_observations IS
    'NOT NULL by design. Sharpe/Sortino are noisy estimates; without n they are unreadable. Any '
    'leaderboard must surface this and suppress rows below a minimum n rather than ranking them.';

-- ── knowledge base ───────────────────────────────────────────────────────────────────────────
-- §3.1: after trades, write the decision, what happened, and what it scored. This is the substrate
-- the judges and personas read back — the loop is not closed until something stores the outcome
-- where the next debate can find it.
CREATE TABLE IF NOT EXISTS knowledge_base_entries (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_type     TEXT        NOT NULL,
    title          TEXT        NOT NULL,
    body           TEXT        NOT NULL,
    lesson         TEXT,

    debate_id      BIGINT,
    portfolio_id   BIGINT,
    evaluation_run_id BIGINT,
    agent_id       BIGINT,
    security_id    BIGINT,

    -- What was knowable when this lesson was written. A lesson replayed into a backtest must respect
    -- it, or the backtest is learning from its own future.
    as_of          TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_kb_debate     FOREIGN KEY (debate_id)     REFERENCES debates (id) ON DELETE SET NULL,
    CONSTRAINT fk_kb_portfolio  FOREIGN KEY (portfolio_id)  REFERENCES paper_portfolios (id) ON DELETE SET NULL,
    CONSTRAINT fk_kb_evaluation FOREIGN KEY (evaluation_run_id) REFERENCES evaluation_runs (id) ON DELETE SET NULL,
    CONSTRAINT fk_kb_agent      FOREIGN KEY (agent_id)      REFERENCES agents (id) ON DELETE RESTRICT,
    CONSTRAINT fk_kb_security   FOREIGN KEY (security_id)   REFERENCES securities (id) ON DELETE RESTRICT,
    CONSTRAINT ck_kb_type CHECK (entry_type IN ('outcome', 'lesson', 'thesis', 'postmortem', 'note'))
);

CREATE INDEX IF NOT EXISTS ix_kb_as_of ON knowledge_base_entries (as_of DESC);
CREATE INDEX IF NOT EXISTS ix_kb_security ON knowledge_base_entries (security_id, as_of DESC)
    WHERE security_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_kb_agent ON knowledge_base_entries (agent_id, as_of DESC)
    WHERE agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_kb_type ON knowledge_base_entries (entry_type, as_of DESC);

DROP TRIGGER IF EXISTS trg_kb_updated_at ON knowledge_base_entries;
CREATE TRIGGER trg_kb_updated_at
    BEFORE UPDATE ON knowledge_base_entries
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE knowledge_base_entries IS
    'EVALUATION_FRAMEWORK §3.1. What was decided, what happened, what it scored, what was learned. '
    'The loop is not closed until the outcome is stored where the next debate can read it.';
COMMENT ON COLUMN knowledge_base_entries.as_of IS
    'What was knowable when this was written. A replay must respect it or the backtest learns from '
    'its own future.';
