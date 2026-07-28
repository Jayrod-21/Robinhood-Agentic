-- 004_evaluation — agents, debates, proposals, judgments, paper portfolios, holdings, daily
-- returns, metrics, guardrail events, risk-free rates, the market calendar, and the knowledge
-- base. The tables the learning loop reads and writes.
--
-- Specified by docs/EVALUATION_FRAMEWORK.md §3-§5. That document explains WHY; this one explains
-- the modelling. Read it first if the shape here looks surprising.
--
-- REVISION NOTE (2026-07-28): rewritten in place during the 004 fix-pass, before any row ever
-- existed. rh-db was rolled back to 000 and re-applied, so the recorded checksum matches this
-- text. Bar §4.5's "never edit an applied migration" protects shared environments; this database
-- is single-operator and held schema only. Findings addressed: REVIEW_schema_004_postgres.md and
-- REVIEW_schema_004_evaluation.md (docs/fixpass/).
--
-- The central idea: a decision is scored by its RISK-ADJUSTED outcome, not by whether it made
-- money. An objective of "20-40% monthly return" with no risk term makes maximum concentration
-- the rational play. Sharpe and Sortino are the correction, and both are carried — they disagree
-- informatively, since Sharpe penalises upside volatility that an aggressive book actively wants.
--
-- Design rule for this migration, learned the hard way in review: a documented guarantee must be
-- ENFORCED at the schema level or it is not a guarantee. Concretely:
--   1. n_observations is NOT NULL on evaluation_runs, and a trigger verifies the claimed n
--      against the actual mark count — an asserted sample size is not a sample size.
--   2. Every as-of anchor is bounded by a constraint, not merely recorded: marks cannot be priced
--      from the future, returns cannot predate inception, a debate's context cannot postdate its
--      start, and a counterfactual cannot be backdated past its debate's cutoff. (Row-level input
--      provenance — WHICH rows a debate read — is future work; these bounds are what the schema
--      can promise today.)
--   3. "Append-only" is a REVOKE, not a comment: the runtime role holds no UPDATE/DELETE on the
--      observation and history tables. The migration role retains full access for surgery.
--
-- The runner owns the transaction: no BEGIN/COMMIT here.

-- ── market calendar ──────────────────────────────────────────────────────────────────────────
-- Reference data: which dates are trading days, and the session bounds. Without it, a gap in
-- portfolio_returns_daily is indistinguishable from a marking failure — n_observations silently
-- absorbs the difference and the annualisation factor inherits it. With it, "the marking job
-- missed three days" is a query. Populated by a loader (NYSE calendar); binding marks to it by FK
-- is deliberately deferred until that loader exists, so an empty calendar cannot block the
-- marking pipeline — the audit query is the consumer for now.
CREATE TABLE IF NOT EXISTS market_calendar (
    trade_date     DATE        PRIMARY KEY,
    is_trading_day BOOLEAN     NOT NULL,
    session_open   TIMESTAMPTZ,
    session_close  TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A trading day has a session; a holiday has none. Half days are just an earlier close.
    CONSTRAINT ck_market_calendar_session CHECK (
        (is_trading_day     AND session_open IS NOT NULL AND session_close IS NOT NULL
                            AND session_close > session_open) OR
        (NOT is_trading_day AND session_open IS NULL AND session_close IS NULL)
    )
);

DROP TRIGGER IF EXISTS trg_market_calendar_updated_at ON market_calendar;
CREATE TRIGGER trg_market_calendar_updated_at
    BEFORE UPDATE ON market_calendar
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE market_calendar IS
    'Trading-day reference (NYSE). Lets a missing daily mark be distinguished from a holiday. '
    'Marks are audited against it by query; an FK binding waits for the calendar loader.';

-- ── risk-free rates ──────────────────────────────────────────────────────────────────────────
-- A stored Sharpe is meaningless without the risk-free rate that produced it, and a rate
-- hardcoded in application code silently reinterprets every historical score when it changes.
-- Point-in-time like 003's fundamentals: known_at is part of the identity, so a revised rate is
-- a NEW observation, never an overwrite.
CREATE TABLE IF NOT EXISTS risk_free_rates (
    series         TEXT        NOT NULL,   -- 'DGS3MO', 'SOFR', …
    effective_date DATE        NOT NULL,
    -- Annual rate as a FRACTION (0.0525 = 5.25%), matching daily_return's convention below.
    annual_rate    NUMERIC(9, 6) NOT NULL,
    known_at       TIMESTAMPTZ NOT NULL,   -- rates get revised; this is the PIT anchor
    source_id      BIGINT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_risk_free_rates_source FOREIGN KEY (source_id)
        REFERENCES data_sources (id) ON DELETE RESTRICT,
    CONSTRAINT ck_risk_free_rates_series CHECK (series ~ '^[A-Z0-9_]{2,32}$'),
    CONSTRAINT ck_risk_free_rates_rate   CHECK (annual_rate BETWEEN -1 AND 1),
    -- Same immutable idiom as 003's ck_fundamentals_known_at: the DATE is anchored to UTC
    -- midnight explicitly, because a bare cast reads the session TimeZone GUC.
    CONSTRAINT ck_risk_free_rates_known  CHECK (known_at >= (effective_date::timestamp AT TIME ZONE 'UTC')),
    CONSTRAINT pk_risk_free_rates PRIMARY KEY (series, effective_date, known_at)
);

-- "Everything this pull produced" — small table, cheap index, same reasoning as 003's
-- ix_fundamentals_source (Bar §4.1: index every FK).
CREATE INDEX IF NOT EXISTS ix_risk_free_rates_source ON risk_free_rates (source_id)
    WHERE source_id IS NOT NULL;

COMMENT ON TABLE risk_free_rates IS
    'Point-in-time risk-free rate observations (fraction, annual). known_at is part of the '
    'identity — a revision is a new row. evaluation_runs.risk_free_annual is sourced from here.';

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
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set when superseded. Never DELETE an agent: its historical proposals and scores must remain
    -- attributable, exactly as delisted securities are retained in 001. Enforced below: rh_app
    -- holds no DELETE on this table, and every child FK is RESTRICT.
    retired_at   TIMESTAMPTZ,

    CONSTRAINT ck_agents_key      CHECK (agent_key ~ '^[a-z][a-z0-9_]{1,48}$'),
    CONSTRAINT ck_agents_kind     CHECK (kind IN ('persona', 'judge', 'blind', 'real')),
    CONSTRAINT ck_agents_version  CHECK (version >= 1),
    CONSTRAINT ck_agents_sha      CHECK (prompt_sha256 IS NULL OR prompt_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_agents_retired  CHECK (retired_at IS NULL OR retired_at >= created_at),
    -- FK target for children that must pin the REFERENCED agent's kind (a judge files judgments,
    -- a persona files proposals). Redundant with the PK as a key; exists so composite FKs can
    -- make "a persona ruled on a debate" unrepresentable rather than a code-review hope.
    CONSTRAINT uq_agents_id_kind  UNIQUE (id, kind)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_key_version ON agents (agent_key, version);
-- At most one live version per key. Two active versions of 'bull' would silently split its record.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_active ON agents (agent_key) WHERE retired_at IS NULL;
-- The blind control is a singleton by design; more than one would make "the control" ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_one_blind ON agents ((kind)) WHERE kind = 'blind' AND retired_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_one_real ON agents ((kind)) WHERE kind = 'real' AND retired_at IS NULL;

DROP TRIGGER IF EXISTS trg_agents_updated_at ON agents;
CREATE TRIGGER trg_agents_updated_at
    BEFORE UPDATE ON agents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE agents IS
    'Persona registry, versioned — a prompt change is a new version, because a track record must not '
    'blend two different reasoners. Agents are retired, never deleted (rh_app holds no DELETE).';
COMMENT ON COLUMN agents.kind IS
    'persona | judge | blind (the unbiased control) | real (the live account, scored on the same '
    'leaderboard).';

-- ── debates ──────────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS debates (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- 'ticker' — one name; 'slate' — the whole book's allocation
    scope        TEXT        NOT NULL,
    security_id  BIGINT,
    -- NOT NULL: a debate whose question was never recorded is unauditable — nothing downstream
    -- can say what the proposals were answers to.
    question     TEXT        NOT NULL,

    -- THE LEAKAGE ANCHOR. Every fact fed to this debate must have been knowable at this instant:
    -- fundamentals filtered on known_at <= context_as_of, bars with ts <= context_as_of. NOT NULL
    -- on purpose — "we forgot to record the cutoff" and "there was no cutoff" must not look the
    -- same — and bounded by ck_debates_context below: a cutoff after the debate started would
    -- mean the debate read the future.
    context_as_of TIMESTAMPTZ NOT NULL,

    status       TEXT        NOT NULL DEFAULT 'running',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_debates_security FOREIGN KEY (security_id)
        REFERENCES securities (id) ON DELETE RESTRICT,
    -- Redundant with ck_debates_scope_security, kept deliberately: a typo'd scope fails THIS
    -- constraint with a readable message instead of the compound one.
    CONSTRAINT ck_debates_scope    CHECK (scope IN ('ticker', 'slate')),
    -- A ticker debate without a security is meaningless; a slate debate with one is a modelling error.
    CONSTRAINT ck_debates_scope_security CHECK (
        (scope = 'ticker' AND security_id IS NOT NULL) OR
        (scope = 'slate'  AND security_id IS NULL)
    ),
    CONSTRAINT ck_debates_status   CHECK (status IN ('running', 'complete', 'failed', 'abandoned')),
    CONSTRAINT ck_debates_complete CHECK (completed_at IS NULL OR completed_at >= started_at),
    -- status and completed_at move together: every terminal status records when it ended, and a
    -- running debate has no end. Untied, 'complete' with NULL completed_at was storable.
    CONSTRAINT ck_debates_status_completed CHECK ((status = 'running') = (completed_at IS NULL)),
    -- Lookahead bound: a context cutoff after the debate began means the debate consumed data
    -- from its own future. A historical replay sets context_as_of in the past — always <= started_at.
    CONSTRAINT ck_debates_context CHECK (context_as_of <= started_at)
);

CREATE INDEX IF NOT EXISTS ix_debates_started ON debates (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_debates_security ON debates (security_id, started_at DESC)
    WHERE security_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_debates_updated_at ON debates;
CREATE TRIGGER trg_debates_updated_at
    BEFORE UPDATE ON debates
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE debates IS
    'One row per debate. Deletable ONLY while nothing downstream was built on it — every scored '
    'artifact (portfolio, marks, metrics) blocks deletion via RESTRICT. Proposals and judgments '
    'of a failed/abandoned debate cascade away with it; that is the cheap-cleanup path.';
COMMENT ON COLUMN debates.context_as_of IS
    'Point-in-time cutoff. Every input must have been knowable at this instant. NOT NULL so a missing '
    'cutoff cannot be mistaken for no cutoff; CHECKed <= started_at so the cutoff cannot postdate '
    'the debate itself.';

-- ── proposals ────────────────────────────────────────────────────────────────────────────────
-- What each persona argued for, WHETHER OR NOT IT WON. This is the seed of every counterfactual
-- track record (§3.3) and the reason a debate yields N observations instead of one: losing
-- proposals are scored too.
CREATE TABLE IF NOT EXISTS agent_proposals (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    debate_id    BIGINT      NOT NULL,
    agent_id     BIGINT      NOT NULL,
    -- Only personas debate. Pinned by the generated column + composite FK: an agents row of any
    -- other kind cannot appear here, however buggy the writer.
    proposer_kind TEXT GENERATED ALWAYS AS ('persona') STORED,
    stance       TEXT        NOT NULL,
    -- 0..1. Stored because a confident wrong call and a hedged wrong call are different failures,
    -- and calibration is only measurable if confidence was recorded at the time.
    confidence   NUMERIC(5, 4),
    -- NOT NULL: an unexplained proposal cannot seed a §3.1 lesson — "entry thesis" is this column.
    rationale    TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_agent_proposals_debate FOREIGN KEY (debate_id)
        REFERENCES debates (id) ON DELETE CASCADE,
    CONSTRAINT fk_agent_proposals_agent FOREIGN KEY (agent_id, proposer_kind)
        REFERENCES agents (id, kind) ON DELETE RESTRICT,
    CONSTRAINT ck_agent_proposals_stance CHECK (stance IN ('buy', 'sell', 'hold', 'abstain')),
    CONSTRAINT ck_agent_proposals_conf   CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    -- FK targets, redundant with the PK as keys. They exist so referencing tables can pin a
    -- proposal TOGETHER WITH the debate/agent it belongs to — without them, a portfolio credited
    -- to bull could be seeded by bear's proposal from a different debate (verified live in review).
    CONSTRAINT uq_agent_proposals_debate_identity UNIQUE (id, debate_id),
    CONSTRAINT uq_agent_proposals_identity UNIQUE (id, debate_id, agent_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_proposals ON agent_proposals (debate_id, agent_id);
CREATE INDEX IF NOT EXISTS ix_agent_proposals_agent ON agent_proposals (agent_id, created_at DESC);

COMMENT ON TABLE agent_proposals IS
    'What each persona proposed in each debate, win or lose — the seed of every counterfactual '
    'track record. Append-only for the runtime role; a failed debate''s proposals cascade away '
    'with the debate.';

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

-- "Counterfactual exposure to name X" is a real query, and the FK needs the index (Bar §4.1) —
-- review verified the unindexed column seq-scanned.
CREATE INDEX IF NOT EXISTS ix_app_security ON agent_proposal_positions (security_id);

COMMENT ON TABLE agent_proposal_positions IS
    'Target weights as a percent of ACCOUNT VALUE (cash included) — the same denominator the '
    'charter''s per-name limit uses. Per-proposal sum is capped at 100 by trg_app_weight_sum.';

-- The per-row CHECK caps each weight at 100; nothing row-local can cap the SUM, and review showed
-- a 300% book was accepted — a persona proposing 3x leverage in a cash account would be marked as
-- if that were real and its Sharpe would read as skill. A deferred constraint trigger checks the
-- aggregate at commit, so a multi-row insert is legal in any order. The 10% cash floor is
-- deliberately NOT enforced here: that is a tunable guardrail (guardrail_events records breaches);
-- sum <= 100 is arithmetic, not policy — you cannot allocate more account than exists.
CREATE OR REPLACE FUNCTION enforce_proposal_weight_sum() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_sum NUMERIC;
BEGIN
    SELECT COALESCE(sum(target_weight_pct), 0) INTO v_sum
    FROM agent_proposal_positions
    WHERE proposal_id = NEW.proposal_id;
    IF v_sum > 100 THEN
        -- Loud and specific (Bar §7.2): name the rule, the row, and the offending value.
        RAISE EXCEPTION 'proposal % target weights sum to % pct of account value; the account only has 100',
            NEW.proposal_id, v_sum
            USING ERRCODE = 'check_violation',
                  HINT = 'Weights are percent of account value (cash included); reduce the allocation.';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_app_weight_sum ON agent_proposal_positions;
CREATE CONSTRAINT TRIGGER trg_app_weight_sum
    AFTER INSERT OR UPDATE ON agent_proposal_positions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_proposal_weight_sum();

-- ── paper portfolios ─────────────────────────────────────────────────────────────────────────
-- The four kinds of book, and how they compare (§3.3-§3.4):
--   counterfactual  — one per (debate, persona): the buy-and-hold sleeve seeded by that proposal.
--   agent_composite — ONE standing book per persona, rebalanced at each new proposal from that
--                     agent. This is what makes "the bull's Sharpe" a well-defined number and the
--                     persona-vs-blind comparison like-for-like — the blind control is also a
--                     standing book, and comparing it against per-debate sleeves would compare
--                     different kinds of object.
--   blind           — the control's standing book.
--   real            — the live account's book.
-- Marking these daily against real prices is what turns "the bear persona seems good" into a number.
CREATE TABLE IF NOT EXISTS paper_portfolios (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          TEXT        NOT NULL,
    -- Every kind of book belongs to an agent (the real account's book belongs to the 'real'
    -- agent), so this is NOT NULL — the shape CHECK previously implied it branch by branch.
    agent_id      BIGINT      NOT NULL,
    debate_id     BIGINT,
    proposal_id   BIGINT,
    -- The owning agent's REQUIRED kind, derived from the book's kind. With the composite FK below
    -- this makes cross-kind attribution unrepresentable: a 'blind' book owned by a persona was
    -- accepted before (verified live in review).
    agent_kind    TEXT GENERATED ALWAYS AS (
        CASE WHEN kind IN ('counterfactual', 'agent_composite') THEN 'persona' ELSE kind END
    ) STORED,
    -- v1 marks counterfactual sleeves as perpetual buy-and-hold — an honest simplification, but
    -- one a reader must SEE, because a buy-and-hold sleeve's Sharpe is not the Sharpe of a
    -- strategy that rebalances. Composites are marked 'rebalanced' and counterfactuals
    -- 'buy_and_hold' — ENFORCED by ck_paper_portfolios_strategy_kind below, because the column
    -- default is 'buy_and_hold' and a composite left at the default would be exactly the
    -- incomparable object this label exists to expose (re-review inserted that pair). A composite
    -- INSERT must say 'rebalanced' explicitly; blind/real state their own mode.
    strategy_mode TEXT        NOT NULL DEFAULT 'buy_and_hold',
    inception_date DATE       NOT NULL,
    -- Portfolios conventionally start at the same notional (this DEFAULT); parity across books is
    -- NOT schema-enforced — no cross-row CHECK exists, and re-review stored 1.00 next to
    -- 100000.00. Ratio metrics (returns, Sharpe) are unaffected by the notional; keeping
    -- notionals equal for absolute-P&L reads is the writing code's responsibility.
    base_value    NUMERIC(18, 2) NOT NULL DEFAULT 100000.00,
    -- Current cash balance, maintained by the marking job (set to base_value at inception).
    -- Makes market_value recomputable (Σ shares x price + cash) and the >=10% cash floor a query.
    cash          NUMERIC(18, 2) NOT NULL DEFAULT 0,
    closed_at     TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_paper_portfolios_agent FOREIGN KEY (agent_id, agent_kind)
        REFERENCES agents (id, kind) ON DELETE RESTRICT,
    -- RESTRICT, not CASCADE: a portfolio is the root of scored history (marks, metrics, lessons).
    -- Review demonstrated one unflagged DELETE FROM debates erasing a full track record — the
    -- exact data the down migration declares unrecoverable. Deleting a debate that produced
    -- portfolios now requires explicitly deleting the portfolios first, which the mark/metric
    -- RESTRICTs below refuse once anything is scored. Loud and overridable, never silent.
    CONSTRAINT fk_paper_portfolios_debate FOREIGN KEY (debate_id)
        REFERENCES debates (id) ON DELETE RESTRICT,
    -- Composite: the proposal must belong to THIS portfolio's debate and agent. Three independent
    -- FKs allowed a portfolio credited to bull in debate 2 to be seeded by bear's proposal from
    -- debate 1 (verified live in review) — every join through such a row mis-attributes a record.
    CONSTRAINT fk_paper_portfolios_proposal FOREIGN KEY (proposal_id, debate_id, agent_id)
        REFERENCES agent_proposals (id, debate_id, agent_id) ON DELETE RESTRICT,
    CONSTRAINT ck_paper_portfolios_kind CHECK (kind IN ('counterfactual', 'agent_composite', 'real', 'blind')),
    CONSTRAINT ck_paper_portfolios_base CHECK (base_value > 0),
    CONSTRAINT ck_paper_portfolios_cash CHECK (cash >= 0),
    CONSTRAINT ck_paper_portfolios_strategy CHECK (strategy_mode IN ('buy_and_hold', 'rebalanced')),
    -- The strategy_mode comment above, made true: a composite IS rebalanced and a counterfactual
    -- IS buy-and-hold, as a constraint rather than a convention. blind/real are deliberately
    -- unconstrained — the control's marking model is a modelling choice, not a fixed fact.
    CONSTRAINT ck_paper_portfolios_strategy_kind CHECK (
        (kind <> 'agent_composite' OR strategy_mode = 'rebalanced') AND
        (kind <> 'counterfactual'  OR strategy_mode = 'buy_and_hold')
    ),
    -- A counterfactual is meaningless without the debate and proposal it came from; every OTHER
    -- kind is a standing book and must NOT carry debate machinery — review showed a blind book
    -- carrying both, which the debate cascade of the day would then have deleted.
    CONSTRAINT ck_paper_portfolios_shape CHECK (
        (kind =  'counterfactual' AND debate_id IS NOT NULL AND proposal_id IS NOT NULL) OR
        (kind IN ('agent_composite', 'blind', 'real') AND debate_id IS NULL AND proposal_id IS NULL)
    ),
    -- UTC-anchored (003's idiom): a bare closed_at::date reads the session TimeZone GUC, so the
    -- same row was accepted under Pacific/Kiritimati and rejected under UTC (verified live in
    -- review) — and a pg_restore under a different TimeZone could fail on accepted rows.
    CONSTRAINT ck_paper_portfolios_closed CHECK (
        closed_at IS NULL OR closed_at >= (inception_date::timestamp AT TIME ZONE 'UTC')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_portfolios_counterfactual
    ON paper_portfolios (debate_id, agent_id) WHERE kind = 'counterfactual';
-- One OPEN standing book per singleton kind. Two open 'real' books were representable before,
-- which would corrupt the leaderboard the kind exists for. Same constant-key partial-unique idiom
-- as uq_agents_one_blind; both kinds fit one index because kind is the indexed key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_portfolios_one_open
    ON paper_portfolios (kind) WHERE kind IN ('blind', 'real') AND closed_at IS NULL;
-- One OPEN composite per persona — the whole point of the composite is that it is THE number.
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_portfolios_one_composite
    ON paper_portfolios (agent_id) WHERE kind = 'agent_composite' AND closed_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_paper_portfolios_agent ON paper_portfolios (agent_id, inception_date DESC);
-- FK indexes (Bar §4.1). The partial predicates are implied by any equality lookup on the column,
-- so RESTRICT enforcement and the "proposals with their realized returns" join both use them —
-- review verified both were seq scans without them (the counterfactual partial unique cannot
-- serve them: its predicate is on kind, which the FK lookup does not imply).
CREATE INDEX IF NOT EXISTS ix_paper_portfolios_debate ON paper_portfolios (debate_id)
    WHERE debate_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_paper_portfolios_proposal ON paper_portfolios (proposal_id)
    WHERE proposal_id IS NOT NULL;

-- Cross-table lookahead bounds that a CHECK cannot express:
--   * a counterfactual incepted BEFORE its debate's context cutoff is a track record manufactured
--     backwards — review backdated one to 2020 against a 2026 debate and it was accepted;
--   * moving inception_date under existing marks would orphan them retroactively.
-- context_as_of is immutable to the runtime role (no UPDATE grant on it), so checking at
-- portfolio-write time is sound.
CREATE OR REPLACE FUNCTION enforce_paper_portfolio_inception() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_ctx_date DATE;
    v_first_mark DATE;
BEGIN
    IF NEW.debate_id IS NOT NULL THEN
        -- UTC-anchored on purpose: ::date inside plpgsql reads the session TimeZone GUC too.
        SELECT (context_as_of AT TIME ZONE 'UTC')::date INTO v_ctx_date
        FROM debates WHERE id = NEW.debate_id;
        IF NEW.inception_date < v_ctx_date THEN
            RAISE EXCEPTION 'portfolio inception % predates debate % context cutoff % — a counterfactual cannot start before the information it was built from',
                NEW.inception_date, NEW.debate_id, v_ctx_date
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.inception_date <> OLD.inception_date THEN
        SELECT min(trade_date) INTO v_first_mark
        FROM portfolio_returns_daily WHERE portfolio_id = NEW.id;
        IF v_first_mark IS NOT NULL AND v_first_mark < NEW.inception_date THEN
            RAISE EXCEPTION 'portfolio % has marks from % — inception_date cannot move past them',
                NEW.id, v_first_mark
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_paper_portfolios_inception ON paper_portfolios;
CREATE TRIGGER trg_paper_portfolios_inception
    BEFORE INSERT OR UPDATE ON paper_portfolios
    FOR EACH ROW EXECUTE FUNCTION enforce_paper_portfolio_inception();

DROP TRIGGER IF EXISTS trg_paper_portfolios_updated_at ON paper_portfolios;
CREATE TRIGGER trg_paper_portfolios_updated_at
    BEFORE UPDATE ON paper_portfolios
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE paper_portfolios IS
    'One book per (debate, persona) counterfactual sleeve, one standing composite per persona, '
    'plus the blind control and the real account. strategy_mode says which marking model a reader '
    'is scoring. Deletion is blocked once marks or metrics exist (RESTRICT).';
COMMENT ON COLUMN paper_portfolios.cash IS
    'Current cash balance maintained by the marking job; set to base_value at inception. With '
    'paper_portfolio_positions this makes market_value recomputable rather than trusted.';

-- ── holdings ─────────────────────────────────────────────────────────────────────────────────
-- The lot ledger. Without it, portfolio_returns_daily.market_value is a number to be TRUSTED —
-- two different marking jobs would both be "correct" — and §3.1's "entry thesis → exit reason →
-- realized P&L → lesson" has no trade to key off. With it, a mark is recomputable:
-- Σ shares x adj_close + cash.
CREATE TABLE IF NOT EXISTS paper_portfolio_positions (
    portfolio_id  BIGINT      NOT NULL,
    security_id   BIGINT      NOT NULL,
    entry_date    DATE        NOT NULL,
    -- A zero-share lot records nothing; fractional shares are real (Robinhood supports them).
    shares        NUMERIC(24, 8) NOT NULL,
    entry_price   NUMERIC(18, 6) NOT NULL,
    exit_date     DATE,
    exit_price    NUMERIC(18, 6),
    exit_reason   TEXT,
    realized_pnl  NUMERIC(18, 2),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_ppp_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES paper_portfolios (id) ON DELETE CASCADE,
    CONSTRAINT fk_ppp_security FOREIGN KEY (security_id)
        REFERENCES securities (id) ON DELETE RESTRICT,
    CONSTRAINT ck_ppp_shares      CHECK (shares > 0),
    CONSTRAINT ck_ppp_entry_price CHECK (entry_price > 0),
    CONSTRAINT ck_ppp_exit_price  CHECK (exit_price IS NULL OR exit_price > 0),
    CONSTRAINT ck_ppp_exit_date   CHECK (exit_date IS NULL OR exit_date >= entry_date),
    -- An exit is a (date, price) pair; recording half of one is a bug either way.
    CONSTRAINT ck_ppp_exit_pair   CHECK ((exit_date IS NULL) = (exit_price IS NULL)),
    -- Exit metadata without an exit is a contradiction.
    CONSTRAINT ck_ppp_exit_meta   CHECK (exit_date IS NOT NULL OR (exit_reason IS NULL AND realized_pnl IS NULL)),
    PRIMARY KEY (portfolio_id, security_id, entry_date)
);

CREATE INDEX IF NOT EXISTS ix_ppp_security ON paper_portfolio_positions (security_id);

DROP TRIGGER IF EXISTS trg_ppp_updated_at ON paper_portfolio_positions;
CREATE TRIGGER trg_ppp_updated_at
    BEFORE UPDATE ON paper_portfolio_positions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE paper_portfolio_positions IS
    'Lot-level holdings per portfolio: entry, exit, realized P&L. Makes market_value recomputable '
    'and gives EVALUATION_FRAMEWORK §3.1 (entry thesis → exit → lesson) its per-position record. '
    'Entries are immutable to the runtime role; only the exit columns are updatable.';

-- ── judgments ────────────────────────────────────────────────────────────────────────────────
-- §3.2 requires a judge to review the outcome of its OWN prior rulings. That needs a join path
-- from a judgment to an outcome, which the first draft of this table did not have — the natural
-- join returned the bull persona's sleeve for a judgment that ruled against it (verified live in
-- review). chosen_proposal_id records WHICH proposal the ruling backed; resulting_portfolio_id
-- records the book the ruling actually produced (typically the real account's), which is the
-- outcome §3.2 scores. The real account's book deliberately carries no debate_id — the account is
-- not per-debate — so THIS is the debate↔account bridge.
CREATE TABLE IF NOT EXISTS judgments (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    debate_id      BIGINT      NOT NULL,
    judge_agent_id BIGINT      NOT NULL,
    -- Only judges judge. Same generated-column + composite-FK enforcement as proposals: a persona
    -- filing a judgment was accepted before (verified live in review).
    judge_kind     TEXT GENERATED ALWAYS AS ('judge') STORED,
    decision       TEXT        NOT NULL,
    chosen_proposal_id     BIGINT,
    resulting_portfolio_id BIGINT,
    confidence     NUMERIC(5, 4),
    rationale      TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_judgments_debate FOREIGN KEY (debate_id)
        REFERENCES debates (id) ON DELETE CASCADE,
    CONSTRAINT fk_judgments_agent FOREIGN KEY (judge_agent_id, judge_kind)
        REFERENCES agents (id, kind) ON DELETE RESTRICT,
    -- Composite: the chosen proposal must belong to THIS debate. NO ACTION rather than RESTRICT
    -- on purpose: deleting a failed debate cascades to BOTH proposals and judgments in one
    -- statement, and NO ACTION defers the check to statement end so that cascade is legal;
    -- RESTRICT would refuse it mid-flight. A direct delete of a still-referenced proposal is
    -- refused either way.
    CONSTRAINT fk_judgments_chosen_proposal FOREIGN KEY (chosen_proposal_id, debate_id)
        REFERENCES agent_proposals (id, debate_id) ON DELETE NO ACTION,
    CONSTRAINT fk_judgments_resulting_portfolio FOREIGN KEY (resulting_portfolio_id)
        REFERENCES paper_portfolios (id) ON DELETE RESTRICT,
    CONSTRAINT ck_judgments_decision CHECK (decision IN ('buy', 'sell', 'hold', 'escalate')),
    CONSTRAINT ck_judgments_conf     CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    -- A directional ruling backs a specific proposal; hold/escalate back nothing.
    CONSTRAINT ck_judgments_chosen   CHECK (decision NOT IN ('buy', 'sell') OR chosen_proposal_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_judgments ON judgments (debate_id, judge_agent_id);
-- §3.2: "show this judge the realized outcome of its own prior judgments." That is this index.
CREATE INDEX IF NOT EXISTS ix_judgments_agent_history ON judgments (judge_agent_id, created_at DESC);
-- FK indexes (Bar §4.1) for the two outcome links.
CREATE INDEX IF NOT EXISTS ix_judgments_chosen_proposal ON judgments (chosen_proposal_id)
    WHERE chosen_proposal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_judgments_resulting_portfolio ON judgments (resulting_portfolio_id)
    WHERE resulting_portfolio_id IS NOT NULL;

COMMENT ON INDEX ix_judgments_agent_history IS
    'Serves EVALUATION_FRAMEWORK §3.2 — a judge reviewing the realized scores of its own prior rulings.';
COMMENT ON TABLE judgments IS
    'Which judge ruled what, why, which proposal it backed, and the portfolio the ruling produced. '
    'judgment → resulting_portfolio → evaluation_runs is the §3.2 self-review join. Append-only '
    'for the runtime role except resulting_portfolio_id, which is set once the book exists.';
COMMENT ON COLUMN judgments.resulting_portfolio_id IS
    'The book this ruling produced — for an executed ruling, the real account''s open portfolio. '
    'Set after the fact (column-level UPDATE grant); the ruling itself is immutable.';

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
    -- Wider than daily_return: the charter's own 20-40%/month target compounds past the old
    -- NUMERIC(12,8) cap (|v| < 10^4) within ~2.5-5 years — the best runs were the unstorable ones.
    cumulative_return NUMERIC(20, 8),
    -- The mark's provenance: which price data produced this row. Lets a leakage audit confirm the
    -- mark used only prices available on the day, and nothing later.
    priced_as_of   TIMESTAMPTZ NOT NULL,
    source_id      BIGINT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT: marks are scored history — the thing the debate-cascade review found being
    -- silently destroyed. Removing a portfolio now means explicitly removing its marks first,
    -- which only the migration role can do.
    CONSTRAINT fk_prd_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES paper_portfolios (id) ON DELETE RESTRICT,
    CONSTRAINT fk_prd_source FOREIGN KEY (source_id)
        REFERENCES data_sources (id) ON DELETE RESTRICT,
    CONSTRAINT ck_prd_value CHECK (market_value >= 0),
    -- A daily return below -100% is impossible for a long-only cash book; -100% exactly is a
    -- book marked to zero (delisting, fraud halt) — a legitimate and analytically important
    -- observation. The old predicate (> -1) banned it while the comment said otherwise.
    CONSTRAINT ck_prd_return CHECK (daily_return IS NULL OR daily_return >= -1),
    -- THE LOOKAHEAD BOUND (verified rejected-then-accepted live in review): a mark priced from
    -- the future is a forecast wearing a Sharpe. Lower bound: the mark cannot predate its own
    -- trading day (UTC-anchored, 003's idiom). Upper bound: trade_date + 4 days covers a long
    -- weekend plus a late provider backfill; tighten with data.
    CONSTRAINT ck_prd_mark_window CHECK (
        priced_as_of >= (trade_date::timestamp AT TIME ZONE 'UTC') AND
        priced_as_of <  ((trade_date + 4)::timestamp AT TIME ZONE 'UTC')
    ),
    PRIMARY KEY (portfolio_id, trade_date)
);

CREATE INDEX IF NOT EXISTS ix_prd_date ON portfolio_returns_daily (trade_date);
-- "Everything this pull produced" — same audit query and same small-table reasoning as 003's
-- ix_fundamentals_source; 002's tens-of-GB objection does not apply at portfolio-mark volume.
CREATE INDEX IF NOT EXISTS ix_prd_source ON portfolio_returns_daily (source_id)
    WHERE source_id IS NOT NULL;

-- A return row before its portfolio existed is a track record manufactured backwards — review
-- inserted a mark six years before inception and it was accepted. Cross-table, so a trigger;
-- inception_date moving under existing marks is refused by trg_paper_portfolios_inception.
CREATE OR REPLACE FUNCTION enforce_prd_mark_after_inception() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_inception DATE;
BEGIN
    SELECT inception_date INTO v_inception
    FROM paper_portfolios WHERE id = NEW.portfolio_id;
    IF NEW.trade_date < v_inception THEN
        RAISE EXCEPTION 'mark for portfolio % dated % predates its inception % — pre-inception returns are manufactured history',
            NEW.portfolio_id, NEW.trade_date, v_inception
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_prd_inception ON portfolio_returns_daily;
CREATE TRIGGER trg_prd_inception
    BEFORE INSERT OR UPDATE ON portfolio_returns_daily
    FOR EACH ROW EXECUTE FUNCTION enforce_prd_mark_after_inception();

COMMENT ON COLUMN portfolio_returns_daily.daily_return IS
    'Fractional (0.0123 = +1.23%), not percent. Mixing the two is how a Sharpe ends up 100x wrong. '
    '-1 exactly = marked to zero; NULL = return undefined (first mark, or prior value was zero).';

-- ── evaluation runs ──────────────────────────────────────────────────────────────────────────
-- Computed metrics for a (portfolio, window). APPEND-ONLY — and enforced: rh_app holds no UPDATE
-- or DELETE here (see the grants section). Recomputing with different reward weights or newer
-- code writes a NEW row; overwriting would rewrite history and void every cross-time comparison.
CREATE TABLE IF NOT EXISTS evaluation_runs (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_id   BIGINT      NOT NULL,
    window_start   DATE        NOT NULL,
    window_end     DATE        NOT NULL,

    -- THE CONSTRAINT THE FRAMEWORK INSISTS ON. NOT NULL prevents omission; trg_evaluation_runs_n
    -- prevents MISSTATEMENT by checking the claim against the actual mark count — review stored
    -- n=5000 against 9 real observations, which a leaderboard's minimum-n suppression would have
    -- trusted.
    n_observations INTEGER     NOT NULL,
    -- The ranking floor IN FORCE when this row was written (tunable config, recorded per the
    -- guardrail rule — never hardcoded in a dashboard query). No default: the writer must state
    -- the policy it applied, or the floor silently becomes whatever the schema guessed.
    min_n_for_ranking INTEGER  NOT NULL,
    -- "Refuse to rank below a minimum n" as a column no UI can forget to check.
    is_rankable    BOOLEAN GENERATED ALWAYS AS (n_observations >= min_n_for_ranking) STORED,

    -- Walk-forward labelling (§3.5, Bar §7.3): a score computed on the data the strategy was
    -- fitted to must never be readable as an honest one. 'live' = production forward marking.
    split          TEXT        NOT NULL DEFAULT 'live',
    experiment_id  BIGINT,
    fold_index     INTEGER,

    -- The parameters that make the ratios REPRODUCIBLE. Review computed the same nine returns to
    -- a Sharpe of 7.20, 7.07, 1.57, or 0.45 depending on rf/annualisation conventions the row did
    -- not record — two such rows are not comparable, which defeats storing them. Fractions,
    -- annual; rf is sourced from risk_free_rates, not asserted from a config constant.
    risk_free_annual NUMERIC(9, 6) NOT NULL,
    mar_annual       NUMERIC(9, 6) NOT NULL DEFAULT 0,
    periods_per_year INTEGER     NOT NULL DEFAULT 252,
    return_frequency TEXT        NOT NULL DEFAULT 'daily',

    -- Ratio columns NUMERIC(18,6): a near-zero downside deviation legitimately produces a huge
    -- Sortino, and the old (12,6) overflowed at 10^6 — the column must not be what decides the
    -- clamp-vs-NULL convention. Return columns NUMERIC(20,8): the charter's own target compounds
    -- past 10^4, and annualizing a short hot window overflows (12,8) trivially.
    sharpe            NUMERIC(18, 6),
    sortino           NUMERIC(18, 6),
    max_drawdown      NUMERIC(12, 8),
    hit_rate          NUMERIC(5, 4),
    avg_win_loss      NUMERIC(18, 6),
    total_return      NUMERIC(20, 8),
    annualized_return NUMERIC(20, 8),
    volatility        NUMERIC(12, 8),
    information_ratio NUMERIC(18, 6),
    -- FK, not free text: 'the vibes index' was storable before. The IR's paired return series
    -- comes from price_bars_daily for this security (SPY et al.).
    benchmark_security_id BIGINT,

    -- The composite the learning loop optimises, plus the weights that produced it. Storing the
    -- weights alongside is what makes a weight change a new observation rather than a silent
    -- rewrite of every past score — so an EMPTY weights object next to a non-null reward is a
    -- contradiction, rejected below.
    reward_total   NUMERIC(18, 6),
    reward_weights JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Names the weight set ('aggressive_v2'), so a config generation is groupable, not just
    -- inspectable.
    reward_config_version TEXT,

    -- Reproducibility: which code computed this, and the latest input timestamp it saw.
    code_version   TEXT,
    inputs_as_of   TIMESTAMPTZ NOT NULL,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT: metrics are scored history (see fk_prd_portfolio's rationale).
    CONSTRAINT fk_evaluation_runs_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES paper_portfolios (id) ON DELETE RESTRICT,
    CONSTRAINT fk_evaluation_runs_benchmark FOREIGN KEY (benchmark_security_id)
        REFERENCES securities (id) ON DELETE RESTRICT,
    CONSTRAINT ck_evaluation_runs_window CHECK (window_end >= window_start),
    CONSTRAINT ck_evaluation_runs_n      CHECK (n_observations >= 0),
    -- A window of D calendar days cannot contain more than D observations, whatever the frequency.
    CONSTRAINT ck_evaluation_runs_n_window CHECK (n_observations <= (window_end - window_start + 1)),
    -- >= 2 because a floor below the arithmetic minimum is not a floor.
    CONSTRAINT ck_evaluation_runs_min_n  CHECK (min_n_for_ranking >= 2),
    CONSTRAINT ck_evaluation_runs_split  CHECK (split IN ('train', 'validation', 'test', 'live')),
    CONSTRAINT ck_evaluation_runs_fold   CHECK (fold_index IS NULL OR fold_index >= 0),
    -- A fold number is meaningless outside an experiment.
    CONSTRAINT ck_evaluation_runs_fold_exp CHECK (fold_index IS NULL OR experiment_id IS NOT NULL),
    CONSTRAINT ck_evaluation_runs_rf     CHECK (risk_free_annual BETWEEN -1 AND 1),
    CONSTRAINT ck_evaluation_runs_mar    CHECK (mar_annual BETWEEN -1 AND 1),
    CONSTRAINT ck_evaluation_runs_ppy    CHECK (periods_per_year BETWEEN 1 AND 366),
    CONSTRAINT ck_evaluation_runs_freq   CHECK (return_frequency IN ('daily', 'weekly', 'monthly')),
    -- Standard deviation is undefined for n < 2, so a Sharpe or Sortino reported with fewer
    -- observations is arithmetically impossible, not merely unreliable. Reject it at the boundary.
    CONSTRAINT ck_evaluation_runs_sharpe_n  CHECK (sharpe  IS NULL OR n_observations >= 2),
    CONSTRAINT ck_evaluation_runs_sortino_n CHECK (sortino IS NULL OR n_observations >= 2),
    -- The supporting metrics get the same arithmetic gate — review stored hit_rate = 1.0 and an
    -- information ratio at n = 1, which is a coin flip wearing four decimals.
    CONSTRAINT ck_evaluation_runs_hit_n  CHECK (hit_rate IS NULL OR n_observations >= 2),
    CONSTRAINT ck_evaluation_runs_ir_n   CHECK (information_ratio IS NULL OR n_observations >= 2),
    CONSTRAINT ck_evaluation_runs_dd_n   CHECK (max_drawdown IS NULL OR n_observations >= 2),
    CONSTRAINT ck_evaluation_runs_ann_n  CHECK (annualized_return IS NULL OR n_observations >= 2),
    CONSTRAINT ck_evaluation_runs_hit_rate  CHECK (hit_rate IS NULL OR hit_rate BETWEEN 0 AND 1),
    -- Drawdown is expressed as a non-positive fraction (-0.23 = a 23% peak-to-trough fall).
    CONSTRAINT ck_evaluation_runs_dd        CHECK (max_drawdown IS NULL OR (max_drawdown <= 0 AND max_drawdown >= -1)),
    CONSTRAINT ck_evaluation_runs_vol       CHECK (volatility IS NULL OR volatility >= 0),
    CONSTRAINT ck_evaluation_runs_awl       CHECK (avg_win_loss IS NULL OR avg_win_loss >= 0),
    -- An information ratio without its benchmark is not reproducible.
    CONSTRAINT ck_evaluation_runs_ir_bench  CHECK (information_ratio IS NULL OR benchmark_security_id IS NOT NULL),
    CONSTRAINT ck_evaluation_runs_weights   CHECK (jsonb_typeof(reward_weights) = 'object'),
    -- A reward with no (or malformed) weights is unobservable — '{}' was the DEFAULT and was
    -- accepted next to a reward, as was {"w_sharpe": "banana"} (verified live in review). All
    -- four §5 terms, numeric, or no reward.
    CONSTRAINT ck_evaluation_runs_reward_weights CHECK (
        reward_total IS NULL OR (
            reward_weights ?& ARRAY['w_sortino', 'w_sharpe', 'w_dd', 'w_breach']
            AND jsonb_typeof(reward_weights -> 'w_sortino') = 'number'
            AND jsonb_typeof(reward_weights -> 'w_sharpe')  = 'number'
            AND jsonb_typeof(reward_weights -> 'w_dd')      = 'number'
            AND jsonb_typeof(reward_weights -> 'w_breach')  = 'number'
        )
    ),
    -- inputs_as_of is a reproducibility anchor only if it relates to the window it describes:
    -- review stored a 2026 window "computed from inputs as of 2020". UTC-anchored (003's idiom).
    CONSTRAINT ck_evaluation_runs_inputs_window CHECK (inputs_as_of >= (window_end::timestamp AT TIME ZONE 'UTC'))
);

CREATE INDEX IF NOT EXISTS ix_evaluation_runs_portfolio
    ON evaluation_runs (portfolio_id, window_end DESC, computed_at DESC);
CREATE INDEX IF NOT EXISTS ix_evaluation_runs_computed ON evaluation_runs (computed_at DESC);
CREATE INDEX IF NOT EXISTS ix_evaluation_runs_benchmark ON evaluation_runs (benchmark_security_id)
    WHERE benchmark_security_id IS NOT NULL;

-- n_observations is verified, not trusted. AFTER (constraint) trigger on purpose: the CHECK
-- constraints above run first, so an arithmetically impossible row fails with its named CHECK
-- rather than this trigger. Only 'daily' rows are verifiable against portfolio_returns_daily —
-- weekly/monthly aggregates are derived series whose count lives with the deriving code.
CREATE OR REPLACE FUNCTION enforce_eval_run_n_observations() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_actual INTEGER;
BEGIN
    IF NEW.return_frequency = 'daily' THEN
        SELECT count(*) INTO v_actual
        FROM portfolio_returns_daily
        WHERE portfolio_id = NEW.portfolio_id
          AND trade_date BETWEEN NEW.window_start AND NEW.window_end
          AND daily_return IS NOT NULL;
        IF v_actual <> NEW.n_observations THEN
            RAISE EXCEPTION 'evaluation run claims n_observations = % but portfolio % has % marks with returns in [%, %]',
                NEW.n_observations, NEW.portfolio_id, v_actual, NEW.window_start, NEW.window_end
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_evaluation_runs_n ON evaluation_runs;
CREATE CONSTRAINT TRIGGER trg_evaluation_runs_n
    AFTER INSERT OR UPDATE ON evaluation_runs
    FOR EACH ROW EXECUTE FUNCTION enforce_eval_run_n_observations();

COMMENT ON TABLE evaluation_runs IS
    'Append-only metric snapshots (ENFORCED: rh_app holds no UPDATE/DELETE). Recomputing with '
    'different weights or newer code writes a NEW row. Each row records the rf/MAR/annualisation '
    'that produced its ratios, so any two rows are comparable — or visibly not.';
COMMENT ON COLUMN evaluation_runs.n_observations IS
    'NOT NULL and VERIFIED against the actual mark count by trg_evaluation_runs_n. Leaderboards '
    'filter on is_rankable, which pins the minimum-n policy in force at write time.';

-- ── guardrail events ─────────────────────────────────────────────────────────────────────────
-- §5's reward subtracts w_breach x guardrail_breach_penalty — and review found the penalty had NO
-- data source anywhere in the database. This is it. It is also the standing ~$4k lesson made
-- durable: every triggered guardrail names the rule, the threshold, the observed value, and what
-- was done about it, so a mis-set limit is one query away instead of a log grep. Overrides carry
-- who and why (tunable, observable, overridable — never a silent block).
CREATE TABLE IF NOT EXISTS guardrail_events (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_id  BIGINT,
    debate_id     BIGINT,
    security_id   BIGINT,
    -- 'cash_floor', 'max_position_pct', 'no_average_down', …
    rule_key      TEXT        NOT NULL,
    severity      TEXT        NOT NULL,
    threshold     NUMERIC(18, 6),
    observed      NUMERIC(18, 6),
    action_taken  TEXT        NOT NULL,
    override_by   TEXT,
    override_reason TEXT,
    inputs        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT: breach history is part of the scored record (it prices w_breach).
    CONSTRAINT fk_guardrail_events_portfolio FOREIGN KEY (portfolio_id)
        REFERENCES paper_portfolios (id) ON DELETE RESTRICT,
    -- SET NULL: a cleaned-up failed debate should not erase the breach observation.
    CONSTRAINT fk_guardrail_events_debate FOREIGN KEY (debate_id)
        REFERENCES debates (id) ON DELETE SET NULL,
    CONSTRAINT fk_guardrail_events_security FOREIGN KEY (security_id)
        REFERENCES securities (id) ON DELETE RESTRICT,
    CONSTRAINT ck_guardrail_events_rule     CHECK (rule_key ~ '^[a-z][a-z0-9_]{1,48}$'),
    CONSTRAINT ck_guardrail_events_severity CHECK (severity IN ('warn', 'block', 'halt')),
    CONSTRAINT ck_guardrail_events_action   CHECK (action_taken IN ('allowed', 'blocked', 'overridden', 'halted')),
    -- An override with no owner is indistinguishable from a bug that skipped the guardrail.
    CONSTRAINT ck_guardrail_events_override CHECK (action_taken <> 'overridden' OR override_by IS NOT NULL),
    CONSTRAINT ck_guardrail_events_inputs   CHECK (jsonb_typeof(inputs) = 'object')
);

-- "Which rule keeps firing, lately" — the mis-set-limit query.
CREATE INDEX IF NOT EXISTS ix_guardrail_events_rule ON guardrail_events (rule_key, occurred_at DESC);
-- FK indexes (Bar §4.1).
CREATE INDEX IF NOT EXISTS ix_guardrail_events_portfolio ON guardrail_events (portfolio_id)
    WHERE portfolio_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_guardrail_events_debate ON guardrail_events (debate_id)
    WHERE debate_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_guardrail_events_security ON guardrail_events (security_id)
    WHERE security_id IS NOT NULL;

COMMENT ON TABLE guardrail_events IS
    'Durable record of every guardrail trip: rule, threshold, observed, action, override. The '
    'data source for EVALUATION_FRAMEWORK §5''s guardrail_breach_penalty, and the query that makes '
    'a mis-set limit visible in seconds. Append-only for the runtime role.';

-- ── knowledge base ───────────────────────────────────────────────────────────────────────────
-- §3.1: after trades, write the decision, what happened, and what it scored. This is the substrate
-- the judges and personas read back — the loop is not closed until something stores the outcome
-- where the next debate can find it.
--
-- APPEND-ONLY, like 003's restatements: a lesson carries as_of ("what was knowable when written"),
-- so editing it later with later knowledge while keeping the old as_of is leakage by edit — the
-- one vector the column exists to close. A correction is a NEW row whose supersedes_id points at
-- the old one; replays follow the chain as of their own cutoff.
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
    supersedes_id  BIGINT,

    -- What was knowable when this lesson was written. A lesson replayed into a backtest must respect
    -- it, or the backtest is learning from its own future.
    as_of          TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_kb_debate     FOREIGN KEY (debate_id)     REFERENCES debates (id) ON DELETE SET NULL,
    CONSTRAINT fk_kb_portfolio  FOREIGN KEY (portfolio_id)  REFERENCES paper_portfolios (id) ON DELETE SET NULL,
    CONSTRAINT fk_kb_evaluation FOREIGN KEY (evaluation_run_id) REFERENCES evaluation_runs (id) ON DELETE SET NULL,
    CONSTRAINT fk_kb_agent      FOREIGN KEY (agent_id)      REFERENCES agents (id) ON DELETE RESTRICT,
    CONSTRAINT fk_kb_security   FOREIGN KEY (security_id)   REFERENCES securities (id) ON DELETE RESTRICT,
    CONSTRAINT fk_kb_supersedes FOREIGN KEY (supersedes_id) REFERENCES knowledge_base_entries (id) ON DELETE RESTRICT,
    CONSTRAINT ck_kb_type CHECK (entry_type IN ('outcome', 'lesson', 'thesis', 'postmortem', 'note'))
);

CREATE INDEX IF NOT EXISTS ix_kb_as_of ON knowledge_base_entries (as_of DESC);
CREATE INDEX IF NOT EXISTS ix_kb_security ON knowledge_base_entries (security_id, as_of DESC)
    WHERE security_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_kb_agent ON knowledge_base_entries (agent_id, as_of DESC)
    WHERE agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_kb_type ON knowledge_base_entries (entry_type, as_of DESC);
-- FK indexes (Bar §4.1): each parent DELETE fires a SET NULL lookup that full-scanned the KB.
CREATE INDEX IF NOT EXISTS ix_kb_debate ON knowledge_base_entries (debate_id)
    WHERE debate_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_kb_portfolio ON knowledge_base_entries (portfolio_id)
    WHERE portfolio_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_kb_evaluation ON knowledge_base_entries (evaluation_run_id)
    WHERE evaluation_run_id IS NOT NULL;
-- A chain, not a tree: each entry has at most one successor (doubles as the FK's index).
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_supersedes ON knowledge_base_entries (supersedes_id)
    WHERE supersedes_id IS NOT NULL;

COMMENT ON TABLE knowledge_base_entries IS
    'EVALUATION_FRAMEWORK §3.1. What was decided, what happened, what it scored, what was learned. '
    'APPEND-ONLY (enforced by grants): a correction is a new row via supersedes_id, because '
    'rewriting a lesson under its original as_of is leakage by edit.';
COMMENT ON COLUMN knowledge_base_entries.as_of IS
    'What was knowable when this was written. A replay must respect it or the backtest learns from '
    'its own future. Immutable together with the rest of the row — corrections supersede.';

-- ── enforced write discipline (the "append-only is a REVOKE" section) ────────────────────────
-- 001's default privileges hand rh_app full DML on every new table; that default is right for
-- ingest tables and wrong for history. Review verified rh_app could rewrite evaluation_runs
-- despite the table comment promising append-only. History tables lose UPDATE and DELETE;
-- mutable-by-design tables keep exactly the columns their lifecycle needs, via column-level
-- grants. The migration role keeps full access for surgery.
REVOKE UPDATE, DELETE ON evaluation_runs, portfolio_returns_daily, agent_proposals,
    agent_proposal_positions, judgments, knowledge_base_entries, guardrail_events,
    risk_free_rates FROM rh_app;
REVOKE UPDATE ON agents, debates, paper_portfolios, paper_portfolio_positions FROM rh_app;
-- Agents are retired, never deleted; calendar rows are corrected, never removed; lot rows leave
-- only when their portfolio does (cascade runs as the table owner, not as rh_app). debates and
-- paper_portfolios deliberately KEEP DELETE: that is the failed-debate cleanup path, and the
-- RESTRICT web above refuses it the moment anything downstream was scored.
REVOKE DELETE ON agents, paper_portfolio_positions, market_calendar FROM rh_app;
GRANT UPDATE (retired_at, display_name, notes) ON agents TO rh_app;
GRANT UPDATE (status, completed_at)            ON debates TO rh_app;
GRANT UPDATE (closed_at, cash)                 ON paper_portfolios TO rh_app;
GRANT UPDATE (exit_date, exit_price, exit_reason, realized_pnl) ON paper_portfolio_positions TO rh_app;
-- The one mutable column on judgments: the outcome link is set once the resulting book exists;
-- the ruling itself is immutable.
GRANT UPDATE (resulting_portfolio_id) ON judgments TO rh_app;
