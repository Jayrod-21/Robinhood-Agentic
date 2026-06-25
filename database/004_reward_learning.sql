-- =============================================================================
-- Reward Learning System — PostgreSQL Schema
-- Migration: 004_reward_learning.sql
--
-- Implements the three-level feedback loop from the Agentic Financial Model
-- transcript (6_23). The core problem: "make 20-40% return" as the only
-- signal causes the model to take maximum risk every time. Sharpe and Sortino
-- ratios give it a risk-adjusted reward signal — it learns that a steady 18%
-- with low volatility beats a chaotic 35% with blowup risk.
--
-- Three feedback loops:
--   1. System  — actual portfolio trades → reward metrics → knowledge base
--   2. Judge   — each juror's vote history → hypothetical returns → self-learning
--   3. Persona — each debate agent's argued positions → hypothetical returns
--
-- All three loops track the same core metrics (Sharpe, Sortino, win rate, etc.)
-- but applied to different populations (real trades vs hypothetical follow-throughs).
-- =============================================================================


-- ---------------------------------------------------------------------------
-- DEBATE PERSONAS
-- Reference table for every agent that participates in debates.
-- The blind agent (is_blind = TRUE) has no persona framework — it is a
-- pure anti-bias, anti-data-leakage control that just tries to make a return.
-- ---------------------------------------------------------------------------

CREATE TABLE agent_personas (
    id          SERIAL          PRIMARY KEY,
    name        VARCHAR(50)     NOT NULL UNIQUE,
    description TEXT,
    is_blind    BOOLEAN         NOT NULL DEFAULT FALSE,
    active      BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  agent_personas IS 'All debate agent personas. is_blind=TRUE marks the unbiased control agent that carries no framework or prompt goal.';
COMMENT ON COLUMN agent_personas.is_blind IS 'TRUE = blind agent: no persona, no framework, no return target in its prompt. Anti-bias control. Should have exactly one row with this flag.';

INSERT INTO agent_personas (name, description, is_blind) VALUES
    ('bull',        'Bullish perspective — argues for upside thesis',                          FALSE),
    ('bear',        'Bearish perspective — argues for downside risks',                         FALSE),
    ('optimistic',  'Macro-optimistic framing — broad market tailwinds',                       FALSE),
    ('pessimistic', 'Macro-pessimistic framing — broad market headwinds',                      FALSE),
    ('wasden',      'Cary Wasden fundamentals-first framework — FCF, Piotroski, conviction',   FALSE),
    ('blind',       'No persona, no framework, no return target. Pure anti-bias control.',     TRUE);


-- ---------------------------------------------------------------------------
-- TRADE OUTCOMES
-- One row per closed position. This is the ground truth that all three
-- reward loops reference when filling in hypothetical / actual results.
-- ---------------------------------------------------------------------------

CREATE TABLE trade_outcomes (
    id                  BIGSERIAL       PRIMARY KEY,
    ticker_id           INTEGER         NOT NULL REFERENCES tickers(id),

    -- Entry / exit
    entry_date          DATE            NOT NULL,
    exit_date           DATE            NOT NULL,
    entry_price         NUMERIC(12,4)   NOT NULL,
    exit_price          NUMERIC(12,4)   NOT NULL,
    shares              NUMERIC(12,4)   NOT NULL,
    direction           VARCHAR(5)      NOT NULL CHECK (direction IN ('long', 'short')),
    holding_days        INTEGER         GENERATED ALWAYS AS (exit_date - entry_date) STORED,

    -- Return
    gross_return_pct    NUMERIC(10,4),       -- (exit - entry) / entry * 100
    net_return_pct      NUMERIC(10,4),       -- after commissions / slippage
    result              VARCHAR(10)     CHECK (result IN ('win', 'loss', 'breakeven')),

    -- Link back to the debate/decision that generated this trade (populated when integrated with 3a)
    debate_id           BIGINT,
    decision_ref        VARCHAR(100),        -- free-form reference until full FK is wired

    notes               TEXT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CHECK (exit_date >= entry_date)
);

COMMENT ON TABLE  trade_outcomes IS 'Ground truth for every closed position. All reward metric calculations and hypothetical backfills reference this table.';
COMMENT ON COLUMN trade_outcomes.holding_days IS 'Computed column: exit_date - entry_date. No application logic needed.';
COMMENT ON COLUMN trade_outcomes.debate_id IS 'FK to the debate that generated this trade. Nullable until full integration with 3a decision pipeline.';

CREATE INDEX idx_trade_ticker_entry ON trade_outcomes (ticker_id, entry_date DESC);
CREATE INDEX idx_trade_exit         ON trade_outcomes (exit_date DESC);


-- ---------------------------------------------------------------------------
-- SYSTEM-LEVEL REWARD METRICS
-- Calculated on a rolling window after each trade closes (or on a cron
-- schedule). Multiple window lengths captured in the same table.
-- These are the numbers that feed back into the knowledge base and
-- influence the system-level prompt / charter over time.
-- ---------------------------------------------------------------------------

CREATE TABLE reward_metrics (
    id                  BIGSERIAL       PRIMARY KEY,
    calculated_at       TIMESTAMPTZ     NOT NULL,
    window_days         SMALLINT        NOT NULL,           -- 30, 90, 365
    trade_count         INTEGER         NOT NULL DEFAULT 0,

    -- Core reward ratios (the ones from the transcript)
    sharpe_ratio        NUMERIC(10,4),  -- (avg_return - risk_free) / std_dev_return
    sortino_ratio       NUMERIC(10,4),  -- (avg_return - risk_free) / downside_deviation

    -- Supporting metrics (needed to compute Sharpe/Sortino; stored for transparency)
    avg_return_pct      NUMERIC(10,4),
    std_dev_return      NUMERIC(10,4),  -- total volatility (Sharpe denominator)
    downside_dev        NUMERIC(10,4),  -- downside-only volatility (Sortino denominator)
    risk_free_rate      NUMERIC(8,4),   -- T-bill rate used at calculation time

    -- Additional reward signal components
    win_rate            NUMERIC(8,4)    CHECK (win_rate BETWEEN 0 AND 1),
    profit_factor       NUMERIC(10,4),  -- gross wins / gross losses
    avg_win_pct         NUMERIC(10,4),
    avg_loss_pct        NUMERIC(10,4),
    max_drawdown_pct    NUMERIC(10,4),
    calmar_ratio        NUMERIC(10,4),  -- annualized_return / max_drawdown

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (calculated_at, window_days)
);

COMMENT ON TABLE  reward_metrics IS 'System-level reward metrics calculated on a rolling window. These are the primary feedback signal into the knowledge base — the model learns risk-adjusted performance, not just raw return.';
COMMENT ON COLUMN reward_metrics.sortino_ratio IS 'Only penalizes downside volatility. Preferred over Sharpe for asymmetric return profiles like this system.';
COMMENT ON COLUMN reward_metrics.risk_free_rate IS 'T-bill rate at calculation time. Store it so historical Sharpe/Sortino values remain interpretable.';

CREATE INDEX idx_reward_metrics_time ON reward_metrics (calculated_at DESC);


-- ---------------------------------------------------------------------------
-- JUDGE VOTE HISTORY
-- One row per juror vote per debate. After the trade closes (or enough
-- time passes), the hypothetical return is backfilled. Judges read their
-- own history to calibrate future votes.
-- ---------------------------------------------------------------------------

CREATE TABLE judge_votes (
    id                          BIGSERIAL       PRIMARY KEY,
    debate_id                   BIGINT          NOT NULL,   -- FK to debate record (3a integration)
    ticker_id                   INTEGER         NOT NULL REFERENCES tickers(id),
    vote_date                   DATE            NOT NULL,

    -- Which judge
    judge_number                SMALLINT        NOT NULL CHECK (judge_number BETWEEN 1 AND 10),
    judge_perspective           VARCHAR(50),                -- e.g. 'fundamentals', 'macro', 'risk', 'wasden'

    -- The vote itself
    vote                        VARCHAR(10)     NOT NULL CHECK (vote IN ('BUY', 'SELL', 'HOLD')),
    confidence                  NUMERIC(5,2)    CHECK (confidence BETWEEN 0 AND 100),
    reasoning_summary           TEXT,

    -- Outcome: backfilled after the position closes
    -- "If I had been followed, what would have happened?"
    hypothetical_return_pct     NUMERIC(10,4),
    hypothetical_result         VARCHAR(10)     CHECK (hypothetical_result IN ('win', 'loss', 'breakeven')),
    outcome_filled_at           TIMESTAMPTZ,
    trade_outcome_id            BIGINT          REFERENCES trade_outcomes(id),

    created_at                  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  judge_votes IS 'Every juror vote with a backfilled hypothetical outcome. Judges read this table to learn from their own track record before casting the next vote.';
COMMENT ON COLUMN judge_votes.hypothetical_return_pct IS 'What the return would have been if this judge''s vote had been followed. Backfilled when the trade closes.';
COMMENT ON COLUMN judge_votes.trade_outcome_id IS 'FK to the actual trade_outcomes row used to compute the hypothetical return.';

CREATE INDEX idx_judge_votes_debate    ON judge_votes (debate_id);
CREATE INDEX idx_judge_votes_ticker    ON judge_votes (ticker_id, vote_date DESC);
CREATE INDEX idx_judge_votes_judge_num ON judge_votes (judge_number, vote_date DESC);


-- ---------------------------------------------------------------------------
-- JUDGE-LEVEL REWARD METRICS
-- Rolled-up reward metrics per judge, per window. Judges read their own
-- row in this table before each debate — it tells them whether their
-- historical calls have been Sharpe-positive or not.
-- ---------------------------------------------------------------------------

CREATE TABLE judge_reward_metrics (
    id              BIGSERIAL       PRIMARY KEY,
    judge_number    SMALLINT        NOT NULL CHECK (judge_number BETWEEN 1 AND 10),
    calculated_at   TIMESTAMPTZ     NOT NULL,
    window_days     SMALLINT        NOT NULL,
    vote_count      INTEGER         NOT NULL DEFAULT 0,

    sharpe_ratio    NUMERIC(10,4),
    sortino_ratio   NUMERIC(10,4),
    win_rate        NUMERIC(8,4)    CHECK (win_rate BETWEEN 0 AND 1),
    avg_return_pct  NUMERIC(10,4),
    max_drawdown_pct NUMERIC(10,4),

    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (judge_number, calculated_at, window_days)
);

COMMENT ON TABLE judge_reward_metrics IS 'Per-judge rolling reward metrics. A judge reads its own row here before voting — self-calibration against past performance.';

CREATE INDEX idx_judge_reward_judge ON judge_reward_metrics (judge_number, calculated_at DESC);


-- ---------------------------------------------------------------------------
-- PERSONA DEBATE POSITIONS
-- One row per persona per debate. After the trade closes, the hypothetical
-- return is backfilled. Each persona accumulates a track record of
-- "if they had done what I said, here is what would have happened."
-- The blind agent's rows are identical in structure — no special treatment.
-- ---------------------------------------------------------------------------

CREATE TABLE persona_debate_positions (
    id                      BIGSERIAL       PRIMARY KEY,
    persona_id              INTEGER         NOT NULL REFERENCES agent_personas(id),
    ticker_id               INTEGER         NOT NULL REFERENCES tickers(id),
    debate_id               BIGINT          NOT NULL,
    debate_date             DATE            NOT NULL,

    -- The position this persona argued for
    argued_direction        VARCHAR(10)     NOT NULL CHECK (argued_direction IN ('bullish', 'bearish', 'neutral')),
    confidence              NUMERIC(5,2)    CHECK (confidence BETWEEN 0 AND 100),
    key_argument_summary    TEXT,

    -- Hypothetical outcome: backfilled after the trade closes
    -- "If they had followed my argument, what would have happened?"
    hypothetical_return_pct NUMERIC(10,4),
    hypothetical_result     VARCHAR(10)     CHECK (hypothetical_result IN ('win', 'loss', 'breakeven')),
    outcome_date            DATE,
    outcome_filled_at       TIMESTAMPTZ,
    trade_outcome_id        BIGINT          REFERENCES trade_outcomes(id),

    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  persona_debate_positions IS 'Every persona''s argued position per debate with a backfilled hypothetical outcome. Personas read their own history to sharpen future arguments. The blind agent is tracked identically.';
COMMENT ON COLUMN persona_debate_positions.argued_direction IS 'The directional stance this persona took in the debate, independent of what the jury decided.';

CREATE INDEX idx_persona_pos_persona  ON persona_debate_positions (persona_id, debate_date DESC);
CREATE INDEX idx_persona_pos_ticker   ON persona_debate_positions (ticker_id, debate_date DESC);
CREATE INDEX idx_persona_pos_debate   ON persona_debate_positions (debate_id);


-- ---------------------------------------------------------------------------
-- PERSONA-LEVEL REWARD METRICS
-- Rolled-up Sharpe/Sortino per persona per window. Each persona reads its
-- own row here before arguing — it knows whether its historical stance on
-- similar setups has been risk-adjusted profitable or not.
-- ---------------------------------------------------------------------------

CREATE TABLE persona_reward_metrics (
    id              BIGSERIAL       PRIMARY KEY,
    persona_id      INTEGER         NOT NULL REFERENCES agent_personas(id),
    calculated_at   TIMESTAMPTZ     NOT NULL,
    window_days     SMALLINT        NOT NULL,
    debate_count    INTEGER         NOT NULL DEFAULT 0,

    sharpe_ratio    NUMERIC(10,4),
    sortino_ratio   NUMERIC(10,4),
    win_rate        NUMERIC(8,4)    CHECK (win_rate BETWEEN 0 AND 1),
    avg_return_pct  NUMERIC(10,4),
    std_dev_return  NUMERIC(10,4),
    downside_dev    NUMERIC(10,4),
    max_drawdown_pct NUMERIC(10,4),

    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (persona_id, calculated_at, window_days)
);

COMMENT ON TABLE persona_reward_metrics IS 'Per-persona rolling reward metrics. Each persona reads its own row before debating — it knows its historical Sharpe and whether its stance tends to be risk-adjusted profitable.';

CREATE INDEX idx_persona_reward_persona ON persona_reward_metrics (persona_id, calculated_at DESC);
