# Review: migration 004 — evaluation-framework fitness

**Reviewer scope:** `db/migrations/004_evaluation.up.sql` and `004_evaluation.destructive.down.sql`,
judged as a **design against `docs/EVALUATION_FRAMEWORK.md`**. SQL validity, naming, and index
mechanics belong to the other reviewer; this review asks only whether the framework can be built on
these tables, and whether the schema stops a trading system from fooling itself.

**Method:** every finding below was demonstrated against the live `rh-db` with 001–004 applied. All
demonstration DDL/DML ran inside a transaction that was rolled back; the nine evaluation tables are
verified empty and all four migrations remain applied (checked after the run). Sequence values
advanced, which is inherent to `GENERATED ALWAYS AS IDENTITY` and harmless.

---

## Summary verdict

**REQUEST CHANGES.**

The bones are right and the intent is unusually well-documented — versioned agents, retire-never-delete,
`context_as_of NOT NULL`, `n_observations NOT NULL`, NUMERIC everywhere, a relational positions table
instead of JSONB. This is a serious schema written by someone who has read the framework.

But it is a schema of **recorded intentions rather than enforced invariants**. The header claims
"every row records the as-of timestamp of its inputs, so a leakage audit is a query rather than an
excavation." The as-of timestamps are present; nothing constrains them. I inserted a daily mark for
`2026-01-20` priced as of `2027-06-01`, a return row six years before its portfolio's inception, a
debate whose `context_as_of` is 1,252 days after it started, and a counterfactual backdated to 2020
against a 2026 debate. All four were accepted without complaint. Migration **003 already contains the
exact immutable-`CHECK` idiom that fixes two of them** — 004 did not carry the pattern across.

Three of the framework's five specified mechanisms cannot be implemented on this schema without a
change: §3.2 (judge self-review) has no join path to any outcome; §3.4 (blind control) is
structurally incomparable to the personas; §5's `guardrail_breach_penalty` has no data source
anywhere in the database.

**9 BLOCKER · 6 SHOULD-FIX · 8 NIT.** Every blocker is an *additive* change — new columns, new
CHECKs, three new tables, one REVOKE. None requires reshaping what is here. Since 004 has never held
a row and `rh-db` is a single-operator local instance, editing 004 in place and re-running
down→up is cleaner than shipping a 005 that patches a never-used 004; Bar §4.5's "never edit an
applied migration" exists to protect shared environments, and this is not one. Record the decision
in the migration header either way.

---

## Can the framework's three placements be implemented on this schema?

### §3.1 — Post-trade feedback → knowledge base: **PARTIALLY**

`knowledge_base_entries` has the right shape for the *portfolio-level* lesson: `debate_id`,
`portfolio_id`, `evaluation_run_id`, `agent_id`, `security_id`, `lesson`, and an `as_of`. Joining a
decision to its score works.

What does not work is the *per-position* form §3.1 and `PROJECT_PLAN.md` Phase 5 both actually
specify — "entry thesis → exit reason → realized P&L → lesson". There is no position record
anywhere in 001–004. The only tables with an FK to `paper_portfolios` are:

```sql
SELECT c.relname FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
WHERE k.confrelid = 'paper_portfolios'::regclass AND k.contype = 'f';
--  portfolio_returns_daily
--  evaluation_runs
--  knowledge_base_entries
```

No holdings, no entries, no exits, no realized P&L, and — for the *real* account — no orders and no
fills table anywhere in the schema. "Post-trade feedback" currently has no trade to key off.

### §3.2 — Judges review the after-effect of their own prior judgments: **NO**

This is the query a judge would need to run, and it does not work:

```sql
-- "the realised Sharpe of decisions I previously made"
SELECT j.id AS judgment_id, j.decision, pp.id AS portfolio_id, pp.kind, er.sharpe
FROM judgments j
LEFT JOIN paper_portfolios pp ON pp.debate_id = j.debate_id
LEFT JOIN evaluation_runs  er ON er.portfolio_id = pp.id
WHERE j.judge_agent_id = :me
ORDER BY j.created_at DESC;
```

Against a single debate with one judgment and one bull counterfactual it returned **two rows for one
judgment** (one per `evaluation_runs` recompute), and the portfolio it returned is *the bull
persona's counterfactual* — not "what the judge decided". The join is possible and it is ambiguous,
which is the worst combination: it will silently return something plausible.

`judgments` columns are exactly `id, debate_id, judge_agent_id, decision, confidence, rationale,
created_at`. There is **no record of which proposal the judge ruled for**. On a `scope='slate'`
debate the ruling is an allocation and `decision IN ('buy','sell','hold','escalate')` cannot express
it at all.

Worse, the outcome a judge actually caused is the *real* account's, and the schema **forbids** the
real portfolio from carrying a debate:

```sql
UPDATE paper_portfolios SET debate_id = (SELECT id FROM debates LIMIT 1) WHERE kind = 'real';
-- ERROR: new row for relation "paper_portfolios" violates check constraint "ck_paper_portfolios_shape"
```

So the only non-counterfactual return series in the database is structurally unlinkable to any
judgment. §3.2 is not implementable as written.

### §3.3 — Per-persona counterfactual track records: **PARTIALLY, and unauditable**

The path proposal → portfolio → daily marks → Sharpe exists and I walked it end to end. What is
missing is everything between "target weights" and "market_value".

Given only `base_value`, `inception_date`, and `agent_proposal_positions.target_weight_pct`, a mark
**cannot be recomputed**: there are no shares, no entry price, no fill convention (close of the
debate day? open of the next?), and no cash balance. `portfolio_returns_daily.market_value` must be
trusted as whatever the marking job wrote, and two different marking jobs would both be "correct".

That would be tolerable if the target weights were sane. They need not be — the aggregate is
unconstrained:

```sql
INSERT INTO agent_proposal_positions (proposal_id, security_id, target_weight_pct)
SELECT p.id, s.id, 100.0 FROM agent_proposals p, securities s WHERE ... ;   -- three names

SELECT p.id, sum(target_weight_pct) AS total_weight_pct,
       100 - sum(target_weight_pct) AS implied_cash_pct
FROM agent_proposal_positions app JOIN agent_proposals p ON p.id = app.proposal_id GROUP BY p.id;
--  proposal_id | total_weight_pct | implied_cash_pct
--            4 |         300.0000 |        -200.0000
```

`ck_app_weight` bounds each row at 100 and says nothing about the sum. A persona can propose a 3×
levered book in a cash account, its counterfactual will be marked as if that were real, and its
Sharpe will look extraordinary. No query in the system can tell that apart from skill. This is
exactly the reward-hacking failure §1 of the framework was written to prevent, arriving through the
data layer instead of the prompt.

The charter's ≥10% cash floor is also unrepresentable: cash is `100 − sum(weights)`, which is
undefined when the sum is unconstrained and unrecorded when it is not.

---

## Findings

### BLOCKER

| # | Finding |
|---|---|
| **B1** | Stored Sharpe/Sortino are **not reproducible** — no risk-free rate, no MAR, no annualisation factor recorded, and no rates table exists |
| **B2** | §3.2 has **no join path from a judgment to an outcome**; the real portfolio is barred from carrying a `debate_id` |
| **B3** | **No holdings table.** `market_value` is unauditable, and proposal weights are unconstrained in aggregate (300% book accepted) |
| **B4** | **Lookahead is unconstrained in four places** — future `priced_as_of`, pre-inception return rows, future `context_as_of`, backdated inception |
| **B5** | `reward_weights` is unvalidated (`{}` is the DEFAULT and is accepted alongside a non-null reward), and §5's `guardrail_breach_penalty` has **no data source in the database at all** |
| **B6** | `n_observations` is **asserted, never verified** — I stored `n = 5000` against 9 actual observations |
| **B7** | **"APPEND-ONLY" is a comment, not a control** — `rh_app` holds `UPDATE` and `DELETE` on `evaluation_runs` |
| **B8** | **No in-sample / out-of-sample label** on `evaluation_runs`; §3.5's walk-forward boundary is unrepresentable |
| **B9** | **Personas and the blind control are not structurally comparable**, and no portfolio in this schema can rebalance |

### SHOULD-FIX

| # | Finding |
|---|---|
| **S1** | No composite FK tying `paper_portfolios.proposal_id` to its `(debate_id, agent_id)` — a portfolio credited to `bull` can be seeded from `bear`'s proposal |
| **S2** | `agents.kind` is not enforced at point of use — a judge can file a proposal, a persona can file a judgment, a `blind` portfolio can belong to a persona |
| **S3** | `knowledge_base_entries` is mutable with a fixed `as_of` — a 2021 lesson can be rewritten in 2026 and still claim 2021 knowability |
| **S4** | `benchmark_symbol` is free text with no FK and no CHECK (`'the vibes index'` accepted), and no benchmark return series exists |
| **S5** | `inputs_as_of` has no relation to `window_end` — a 2026 window computed from inputs "as of" 2020 is accepted |
| **S6** | No market-calendar / trading-day reference, so a gap in `portfolio_returns_daily` cannot be distinguished from a marking failure |

### NIT

| # | Finding |
|---|---|
| **N1** | `annualized_return NUMERIC(12,8)` overflows on short windows — a 2-day +10% run annualises to 164,238.77; the column caps at 9,999.99999999 |
| **N2** | `volatility` and `avg_win_loss` have no non-negativity CHECK |
| **N3** | `market_value >= 0` permits 0, after which any `daily_return` is a division by zero |
| **N4** | `judgments.decision` cannot express a ruling on a `scope='slate'` debate |
| **N5** | `agent_proposals.rationale` and `debates.question` are nullable — an unexplained proposal is unauditable |
| **N6** | `knowledge_base_entries` has no unique key; duplicate outcome rows for the same `(debate, portfolio, evaluation_run)` are possible |
| **N7** | `uq_agents_one_blind` / `uq_agents_one_real` index the expression `((kind))`; correct, but a partial unique on a constant reads oddly — `((true))` says the same thing |
| **N8** | `ix_prd_date` is unvalidated against a real plan; per Bar §4.4 it wants an `EXPLAIN (ANALYZE, BUFFERS)` once marks exist |

### PRAISE

- **`debates.context_as_of NOT NULL`, with the reasoning in the comment.** "We forgot to record the
  cutoff" and "there was no cutoff" genuinely must not look the same. Right call, right rationale.
- **Agents are versioned and retired, never deleted** — and the comment ties it to 001's
  delisted-securities decision. That consistency of principle across migrations is what makes a
  schema readable a year later.
- **`agent_proposal_positions` as a real table, not JSONB**, with the denominator (account value,
  cash included) pinned in a `COMMENT` *and* cross-referenced to the dashboard's conflicting
  share-of-equity denominator (issue #21). That is precisely the kind of latent unit bug that eats a
  week, caught before any data exists.
- **`daily_return` documented as fractional** with the reason stated ("how a Sharpe ends up 100×
  wrong"). Unit conventions written down at the column are worth more than any test.
- **`n_observations NOT NULL` plus `sharpe IS NULL OR n >= 2`** — gating the *ratio* on n rather than
  gating the row is the right shape, even though the floor is too low (B6).
- **`max_drawdown` sign convention pinned by CHECK** (`<= 0 AND >= -1`). Ambiguous drawdown signs are
  a perennial source of silently-inverted leaderboards.
- **`ck_paper_portfolios_shape` existing at all.** Most schemas would have left the kind/nullability
  relationship to application code. The instinct is right; B9 is about its content, not its presence.
- **The down migration is honest about what it destroys** — "the counterfactual track records are NOT
  recoverable by replay" is true and most authors would not have written it.

---

## Detailed findings

### B1 — Stored ratios are not reproducible: no rf, no MAR, no annualisation, no rates table

Sharpe and Sortino *are* computable from `portfolio_returns_daily`. Here is the query, run against a
9-observation series:

```sql
WITH params AS (
    SELECT 0.0525::numeric AS rf_annual,    -- <<< hardcoded; nowhere in the schema
           0.00::numeric   AS mar_annual,   -- <<< hardcoded; nowhere in the schema
           252::numeric    AS periods_per_year
), r AS (
    SELECT prd.daily_return AS ret
    FROM portfolio_returns_daily prd
    JOIN paper_portfolios pp ON pp.id = prd.portfolio_id
    WHERE pp.id = :portfolio_id
      AND prd.trade_date BETWEEN :window_start AND :window_end
      AND prd.daily_return IS NOT NULL
), x AS (
    SELECT r.ret, p.rf_annual/p.periods_per_year AS rf_d,
           p.mar_annual/p.periods_per_year AS mar_d, p.periods_per_year AS ppy
    FROM r CROSS JOIN params p
)
SELECT count(*) AS n_observations,
       ((avg(ret) - min(rf_d))  / stddev_samp(ret))                        * sqrt(min(ppy)) AS sharpe,
       ((avg(ret) - min(mar_d)) / sqrt(avg(power(least(ret - mar_d,0),2)))) * sqrt(min(ppy)) AS sortino
FROM x;
--  n_observations | mean_daily | sd_daily   | sharpe   | sortino
--               9 | 0.01196900 | 0.02639703 | 7.072567 | 19.879823
```

So the observation table is adequate. The problem is `evaluation_runs`. The **same nine returns**
produce materially different stored numbers depending on three parameters that the row does not
record:

| assumption | sharpe |
|---|---|
| rf = 0 bp, annualised √252 | **7.1979** |
| rf = 525 bp, annualised √252 | **7.0726** |
| annualised √12 (monthly periodicity) | **1.5707** |
| not annualised | **0.4534** |

I inserted 7.0726 and 3.906 into `evaluation_runs` for the identical `(portfolio_id, window_start,
window_end, n_observations, code_version)` and nothing in either row distinguishes them. The rf gap
alone is ~0.13 Sharpe here and grows with volatility; the annualisation gap is a factor of 15.9.
Between a 2021 window (rf ≈ 0) and a 2024 window (rf ≈ 5.25%) an *unrecorded* rf convention makes
cross-time comparison — the explicit purpose of the append-only design — meaningless.

Framework §5 already states the principle: *"every computed reward is stored with the weights that
produced it, and a change of weights is a new `evaluation_runs` row rather than a silent overwrite."*
The schema applies that discipline to the four reward weights and not to the three parameters that
determine the ratios those weights multiply. That is the wrong way round: `w_sharpe` is a policy
knob, `rf` is an input.

Is hardcoding in application code acceptable? No — for exactly the reason the framework gives about
weights. A constant in Python is not versioned alongside the row it produced, so a rate change
silently reinterprets every historical score.

**Fix.** On `evaluation_runs`:

```sql
risk_free_annual  NUMERIC(9,6)  NOT NULL,
mar_annual        NUMERIC(9,6)  NOT NULL DEFAULT 0,
periods_per_year  INTEGER       NOT NULL DEFAULT 252 CHECK (periods_per_year > 0),
return_frequency  TEXT          NOT NULL DEFAULT 'daily'
                  CHECK (return_frequency IN ('daily','weekly','monthly'))
```

And a small point-in-time rates table so the rf is *sourced*, not asserted — it composes with 003's
`known_at` pattern and gives §3.5's as-of-timestamp rule something to bite on:

```sql
CREATE TABLE risk_free_rates (
    series        TEXT        NOT NULL,          -- 'DGS3MO', 'SOFR', …
    effective_date DATE       NOT NULL,
    annual_rate   NUMERIC(9,6) NOT NULL,
    known_at      TIMESTAMPTZ NOT NULL,          -- rates are revised; this is the PIT anchor
    source_id     BIGINT REFERENCES data_sources (id) ON DELETE RESTRICT,
    PRIMARY KEY (series, effective_date, known_at)
);
```

### B2 — §3.2 has no join path to an outcome

Demonstrated above. **Fix:**

- `judgments.chosen_proposal_id BIGINT REFERENCES agent_proposals (id)` — nullable, since `hold` and
  `escalate` choose nothing, with a CHECK that it is NOT NULL when `decision IN ('buy','sell')`.
- `judgments.resulting_portfolio_id BIGINT REFERENCES paper_portfolios (id)` — the portfolio the
  ruling actually produced, which is what §3.2 wants to score.
- Relax `ck_paper_portfolios_shape` so a `real` portfolio may carry a `debate_id`, **or** introduce
  an `account_decisions` table linking a debate + judgment to the real account's position changes.
  The current CHECK is a deliberate modelling statement — "the live account is not per-debate" — and
  it is defensible, but then something else must carry the link, and nothing does.

### B3 — No holdings; `market_value` unauditable; weights unbounded in aggregate

Demonstrated above. **Fix:**

```sql
CREATE TABLE paper_portfolio_positions (
    portfolio_id  BIGINT NOT NULL REFERENCES paper_portfolios (id) ON DELETE CASCADE,
    security_id   BIGINT NOT NULL REFERENCES securities (id) ON DELETE RESTRICT,
    shares        NUMERIC(24,8) NOT NULL CHECK (shares >= 0),
    entry_date    DATE   NOT NULL,
    entry_price   NUMERIC(18,6) NOT NULL CHECK (entry_price > 0),
    exit_date     DATE,
    exit_price    NUMERIC(18,6) CHECK (exit_price IS NULL OR exit_price > 0),
    exit_reason   TEXT,
    realized_pnl  NUMERIC(18,2),
    PRIMARY KEY (portfolio_id, security_id, entry_date)
);
ALTER TABLE paper_portfolios ADD COLUMN cash NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK (cash >= 0);
```

That makes `market_value` a recomputable quantity (`Σ shares × adj_close + cash`) rather than a
number to be trusted, gives §3.1's entry-thesis→exit→lesson a home, and makes the ≥10% cash floor a
query.

For the weight sum, Postgres cannot express a cross-row CHECK. Two workable options:

1. `agent_proposals.total_weight_pct NUMERIC(7,4) NOT NULL CHECK (total_weight_pct BETWEEN 0 AND 100)`
   maintained by a `CONSTRAINT TRIGGER … DEFERRABLE INITIALLY DEFERRED` on
   `agent_proposal_positions`, so the invariant is checked at commit and a multi-row insert is legal.
2. Write positions as a single set-valued statement and validate in a deferred constraint trigger
   directly.

Either way, name the invariant and make the violation loud — per Bar §7.2 a blocked write must
announce *which* rule and *what* input, which a bare CHECK message does not do. The trigger should
`RAISE EXCEPTION` with the proposal id and the offending sum.

### B4 — Lookahead is unconstrained in four places

All four accepted by the live database:

```sql
-- 4a. A mark for 2026-01-20 priced as of 2027-06-01. Accepted.
INSERT INTO portfolio_returns_daily (portfolio_id, trade_date, market_value, daily_return, priced_as_of)
VALUES (:p, DATE '2026-01-20', 150000.00, 0.35, '2027-06-01T00:00:00Z');
--  trade_date | priced_as_of           | daily_return | mark_lag
--  2026-01-20 | 2027-06-01 00:00:00+00 |   0.35000000 | 1 year 4 mons 12 days

-- 4b. A return row five years before the portfolio's inception. Accepted.
--  inception_date | trade_date | daily_return
--  2026-01-05     | 2021-03-01 |   0.02000000

-- 4c. A debate whose context_as_of is 1,252 days after started_at. Accepted.
--  started_at                     | context_as_of          | lookahead
--  2026-07-28 21:37:02.687909+00  | 2030-01-01 00:00:00+00 | 1252 days 02:22:57

-- 4d. A counterfactual backdated to 2020-01-01 against a debate with context_as_of 2026-01-05. Accepted.
```

4a is the direct one: mark today's portfolio with next year's prices and the Sharpe is a forecast,
not a measurement. 4b and 4d are the more insidious pair — they let a track record be *manufactured
backwards* after the fact, which is survivorship bias's more deliberate cousin.

**This is fixable in one line each, using an idiom that already exists in migration 003.** 003's
`ck_fundamentals_known_at` uses `(period_end::timestamp AT TIME ZONE 'UTC')` precisely because a bare
`::timestamptz` cast is not `IMMUTABLE` and reads the session `TimeZone` GUC. I verified the
equivalent constraints are accepted by PG16 by adding them to the live tables (then rolling back):

```sql
ALTER TABLE portfolio_returns_daily ADD CONSTRAINT ck_prd_mark_not_future CHECK (
    priced_as_of >= (trade_date::timestamp AT TIME ZONE 'UTC')
AND priced_as_of <  ((trade_date + 4)::timestamp AT TIME ZONE 'UTC'));   -- ALTER TABLE ✓

ALTER TABLE debates ADD CONSTRAINT ck_debates_context_not_future
    CHECK (context_as_of <= started_at);                                  -- ALTER TABLE ✓
```

(The `+ 4` day grace covers a long weekend plus a late provider backfill; tighten with data.)

4b and 4d are cross-table and need constraint triggers: `portfolio_returns_daily.trade_date >=
paper_portfolios.inception_date`, and `paper_portfolios.inception_date >= debates.context_as_of::date`
for counterfactuals.

**On the broader question — is `context_as_of` sufficient?** It is *necessary* and not sufficient,
and the schema's header comment slightly oversells it. `context_as_of` is recorded and trusted:
nothing links a debate to the rows it actually consumed. The only tables referencing `debates` are
`agent_proposals`, `judgments`, `paper_portfolios`, `knowledge_base_entries` — all outputs, no inputs.
A leakage audit of "did debate #17 read a fundamentals row with `known_at > context_as_of`?" is not a
query; it is unanswerable. If the framework's §3.5 claim that leakage is "enforced in the feature
store, not by convention" is to hold, there needs to be a `debate_inputs` table recording
`(debate_id, source_table, source_row_id, row_as_of)` — or at minimum a materialised feature table
carrying its own as-of that debates reference by id.

### B5 — Reward composition is unvalidated, and one of its four terms has no data

```sql
-- reward_total with NO weights at all — and '{}' is the column DEFAULT. Accepted.
--  id | reward_total | reward_weights
--   4 |     3.912000 | {}
--   5 |   999.000000 | {"w_sharpe": "banana", "totally_unrelated": [1, 2, 3]}
```

`ck_evaluation_runs_weights` checks only `jsonb_typeof(...) = 'object'`. So yes — a reward can be
stored whose weights do not match what produced it, including the case where no weights are recorded
at all, which is the *default path*. Given the standing rule that risk controls be tunable **and
observable**, JSONB is a reasonable container but an unvalidated one is not adequate: observability
that permits `"banana"` is not observability.

```sql
ALTER TABLE evaluation_runs ADD CONSTRAINT ck_eval_reward_weighted
    CHECK (reward_total IS NULL OR reward_weights <> '{}'::jsonb);        -- ALTER TABLE ✓ (verified)
ALTER TABLE evaluation_runs ADD CONSTRAINT ck_eval_reward_weight_keys CHECK (
    reward_total IS NULL OR (
        reward_weights ?& ARRAY['w_sortino','w_sharpe','w_dd','w_breach']
    AND jsonb_typeof(reward_weights->'w_sortino') = 'number'
    AND jsonb_typeof(reward_weights->'w_sharpe')  = 'number'
    AND jsonb_typeof(reward_weights->'w_dd')      = 'number'
    AND jsonb_typeof(reward_weights->'w_breach')  = 'number'));
```

Also add a `reward_config_version TEXT` so the weight set is nameable, not just inspectable.

**The second half of this blocker is worse.** §5's reward has four terms and the fourth,
`guardrail_breach_penalty`, has no data source:

```sql
SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p')
  AND (c.relname LIKE '%guardrail%' OR c.relname LIKE '%breach%' OR c.relname LIKE '%risk%');
--  0
```

So the reward the learning loop optimises cannot be computed as specified, and the term that "keeps
the ≥10% cash floor and the ≤25% per-name limit inside the objective rather than bolted on outside
it" is the one that is missing. This is also the ~$4k observability lesson: Phase 5 requires "a log
line naming the rule and the input whenever one fires", and a log line is not queryable history.

```sql
CREATE TABLE guardrail_events (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_id BIGINT REFERENCES paper_portfolios (id) ON DELETE CASCADE,
    debate_id    BIGINT REFERENCES debates (id) ON DELETE SET NULL,
    security_id  BIGINT REFERENCES securities (id) ON DELETE RESTRICT,
    rule_key     TEXT        NOT NULL,   -- 'cash_floor', 'max_position_pct', 'no_average_down'
    severity     TEXT        NOT NULL CHECK (severity IN ('warn','block','halt')),
    threshold    NUMERIC(18,6),
    observed     NUMERIC(18,6),
    action_taken TEXT        NOT NULL CHECK (action_taken IN ('allowed','blocked','overridden','halted')),
    override_by  TEXT,                    -- who overrode, per the overridable rule
    override_reason TEXT,
    inputs       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

That makes `w_breach` computable, makes a mis-set guardrail visible in one query, and satisfies
"observable + overridable" as durable state rather than log grep.

### B6 — `n_observations` is asserted, never verified; and n ≥ 2 is only the arithmetic floor

```sql
INSERT INTO evaluation_runs (..., n_observations, sharpe, sortino, ...) VALUES (..., 5000, 4.235, 9.111, ...);
--  claimed_n | actual_n
--       5000 |        9
```

The framework's flagship schema constraint — `n_observations NOT NULL` — prevents *omission*, not
*misstatement*. A leaderboard that suppresses under-powered rows is trivially defeated by a marking
job with an off-by-N bug or a replay that double-counts.

On the floor: n ≥ 2 is correct as an arithmetic gate (stddev is undefined below it) and is far below
the useful floor. Standard error of a Sharpe estimate, at a true SR of 2:

| n | SE(Sharpe) |
|---|---|
| 2 | 1.225 |
| 5 | 0.775 |
| 21 | 0.378 |
| 63 | 0.218 |
| 126 | 0.154 |
| 252 | 0.109 |
| 504 | 0.077 |
| 1260 | 0.049 |

At n = 2 the estimate's standard error exceeds half its own value — exactly the "six days of noise
read as skill" the column comment warns about, permitted by the constraint below it. And the
supporting metrics have **no** n gate at all: I stored `hit_rate = 1.0000` and `information_ratio =
4.4` at n = 1.

**Fix.** Three parts:

1. A constraint trigger (or `n_observations_verified_at TIMESTAMPTZ`) checking the claim against
   `count(*) FROM portfolio_returns_daily WHERE portfolio_id = … AND trade_date BETWEEN window_start
   AND window_end AND daily_return IS NOT NULL`.
2. Extend the n gate: `CHECK (hit_rate IS NULL OR n_observations >= 2)`,
   `CHECK (information_ratio IS NULL OR n_observations >= 2)`,
   `CHECK (max_drawdown IS NULL OR n_observations >= 2)`.
3. Support the *policy* floor the framework demands. The rankable threshold must be tunable and
   recorded, not hardcoded in a dashboard query — a `min_n_for_ranking INTEGER NOT NULL` on
   `evaluation_runs` (the floor in force when the row was written) plus a generated
   `is_rankable BOOLEAN GENERATED ALWAYS AS (n_observations >= min_n_for_ranking) STORED`. Then
   "refuse to rank below a minimum n" is a `WHERE is_rankable` that no future UI can forget.

### B7 — "APPEND-ONLY" is a comment, not a control

```sql
SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type)
FROM information_schema.role_table_grants WHERE grantee = 'rh_app' GROUP BY table_name;
--  agent_proposals         | DELETE,INSERT,SELECT,UPDATE
--  evaluation_runs         | DELETE,INSERT,SELECT,UPDATE
--  judgments               | DELETE,INSERT,SELECT,UPDATE
--  portfolio_returns_daily | DELETE,INSERT,SELECT,UPDATE
```

001's `ALTER DEFAULT PRIVILEGES … GRANT SELECT, INSERT, UPDATE, DELETE` is a sensible default that
004 inherits silently, and it directly contradicts the `COMMENT ON TABLE evaluation_runs` promise
that "recomputing with different weights writes a NEW row — overwriting would rewrite history and
void every cross-time comparison." The runtime role can rewrite history today. Bar §7.2 requires an
"immutable append-only audit log of every decision/signal/order/fill/rejection".

```sql
REVOKE UPDATE, DELETE ON evaluation_runs, portfolio_returns_daily,
                         agent_proposals, agent_proposal_positions, judgments FROM rh_app;
```

(`debates` legitimately needs `UPDATE` for `status`/`completed_at`; consider a column-level grant.
`knowledge_base_entries` carries an `updated_at` trigger, so mutability there is deliberate — see S3.)

### B8 — No in-sample / out-of-sample label

`evaluation_runs` columns in full: `id, portfolio_id, window_start, window_end, n_observations,
sharpe, sortino, max_drawdown, hit_rate, avg_win_loss, total_return, annualized_return, volatility,
information_ratio, benchmark_symbol, reward_total, reward_weights, code_version, inputs_as_of,
computed_at`.

Nothing distinguishes a score computed on a walk-forward *test* fold from one computed on the data
the strategy was fitted to. §3.5 makes walk-forward with "a hard train/test boundary" a structural
defence, and Bar §7.3 makes split-before-fitting a **[P0]**. The single most important qualifier on
any number in this table cannot be recorded.

```sql
split       TEXT    NOT NULL DEFAULT 'live'
            CHECK (split IN ('train','validation','test','live')),
experiment_id BIGINT,        -- groups the folds of one walk-forward run
fold_index    INTEGER CHECK (fold_index IS NULL OR fold_index >= 0)
```

### B9 — Personas and the blind control are not structurally comparable; nothing rebalances

`ck_paper_portfolios_shape` is asymmetric, and the asymmetry decides the answer to §3.4:

```sql
-- a standing persona portfolio (no debate) — REJECTED
INSERT INTO paper_portfolios (kind, agent_id, inception_date) VALUES ('counterfactual', :bull, DATE '2026-01-05');
-- ERROR: violates check constraint "ck_paper_portfolios_shape"

-- a standing blind portfolio (no debate) — ACCEPTED
INSERT INTO paper_portfolios (kind, agent_id, inception_date) VALUES ('blind', :blind, DATE '2026-01-05');
```

So the blind agent gets **one continuous portfolio with one long return series and one well-defined
Sharpe**, while each persona gets **one perpetual buy-and-hold sleeve per debate**, and the schema
provides no rule for combining sleeves into a persona-level number: no capital allocation across
sleeves, no weighting, no composite. "The bull's Sharpe" is undefined; "the blind's Sharpe" is
defined. §3.4 asks "does the debate machinery beat a plain agent?" — on this schema that comparison
is between two different kinds of object, and would be invalid however carefully it is computed.

The comparison would additionally be invalidated by: different `inception_date`s (nothing aligns
them), different evaluation windows (nothing links `evaluation_runs` rows into a comparison set),
and different `base_value`s — I set the blind portfolio's `base_value` to 1.00 while the personas sat
at 100,000 and it was accepted. Base value does not affect a ratio, but it does affect lot sizing,
the cash floor, and any dollar-denominated comparison.

**On rebalancing — this is a limitation, not a deliberate simplification, and it will force a
rework.** Target weights are fixed at proposal time; there is no transactions table, no
weight-over-time, and `uq_paper_portfolios_counterfactual` pins one portfolio per `(debate, agent)`.
A persona that says "buy NVDA" on Monday and "sell NVDA" on Friday produces two independent,
overlapping, immortal buy-and-hold sleeves — not a strategy, and its measured Sharpe is not the
Sharpe of anything the persona would actually have done. Nothing even determines when `closed_at`
should be set. Framework §3.3 says the counterfactual is "what would have happened had its
recommendation been followed," and following a sequence of recommendations means rebalancing.

I would not call the buy-and-hold model wrong as a *v1* — it is the honest starting point. But it
must be labelled as such in the schema rather than implied, and the extension path must exist:

- `paper_portfolios.strategy_mode TEXT NOT NULL CHECK (strategy_mode IN ('buy_and_hold','rebalanced'))`,
  so a reader knows which object they are scoring.
- A standing, agent-level portfolio for each persona (relax the shape CHECK so `counterfactual`
  permits `debate_id IS NULL` when it is an agent-level composite, or add
  `kind = 'agent_composite'`), rebalanced at each new proposal from that agent. That single change
  makes personas and the blind agent the same kind of object, which is what §3.4 needs.
- The `paper_portfolio_positions` table from B3 with `entry_date`/`exit_date` gives you
  holdings-over-time for free once it exists.

### S1 — Attribution can be crossed

```sql
UPDATE paper_portfolios SET proposal_id = (bear's proposal) WHERE kind = 'counterfactual';
--   id  | credited_to | proposal_actually_from | stance
--  3013 | bull        | bear                   | sell
```

`paper_portfolios` has three independent FKs (`agent_id`, `debate_id`, `proposal_id`) and nothing
ties them together, so a track record can be credited to the wrong persona — which corrupts the
exact quantity §3.3 exists to measure. The standard relational fix:

```sql
ALTER TABLE agent_proposals ADD CONSTRAINT uq_agent_proposals_identity UNIQUE (id, debate_id, agent_id);
ALTER TABLE paper_portfolios ADD CONSTRAINT fk_paper_portfolios_proposal_identity
    FOREIGN KEY (proposal_id, debate_id, agent_id)
    REFERENCES agent_proposals (id, debate_id, agent_id) ON DELETE CASCADE;
```

### S2 — `agents.kind` is not enforced at point of use

```sql
--  row_kind        | agent_key  | agent_kind
--  blind portfolio | bear       | persona     <- a 'blind' portfolio owned by a persona
--  judgment        | bull       | persona     <- a persona ruling on a debate
--  proposal        | judge_risk | judge       <- a judge filing a proposal
```

All three accepted. The `kind` taxonomy is load-bearing for the whole framework — it is what makes
"the blind control" a control — and it is advisory. Fix with `UNIQUE (id, kind)` on `agents`, a
generated constant column on each child (e.g. `agent_kind TEXT GENERATED ALWAYS AS ('judge') STORED`
on `judgments`), and a composite FK; or a constraint trigger if the generated-column approach reads
as too clever.

### S3 — Knowledge-base lessons are mutable with a frozen `as_of`

`trg_kb_updated_at` makes `knowledge_base_entries` a mutable table, but `as_of` is a plain column
with no trigger maintaining it. So a lesson written in 2021 can be rewritten in 2026 with 2026
knowledge and still assert it was knowable in 2021 — and the column comment says a replay "must
respect it or the backtest learns from its own future." That is leakage by edit, and it is the one
leakage vector the schema's own comment claims to have closed. Either make the table append-only with
a `supersedes_id` chain (consistent with 003's restatement-is-a-new-row decision, which is the right
precedent), or have the trigger advance `as_of` on update.

### S4 — Benchmark is unmodelled

```sql
INSERT INTO evaluation_runs (..., information_ratio, benchmark_symbol, ...) VALUES (..., 1.2, 'the vibes index', ...);
--  benchmark_symbol | information_ratio
--  the vibes index  |          1.200000
```

`information_ratio` and `benchmark_symbol` exist; the benchmark **series** does not. Information
ratio needs a paired return series (portfolio − benchmark, then tracking error), and there is nowhere
to get one that is guaranteed aligned to the portfolio's trading days. `price_bars_daily` will hold
SPY *if* SPY is ingested, but `benchmark_symbol TEXT` has no FK to `securities`, so the linkage is by
string. Replace with `benchmark_security_id BIGINT REFERENCES securities (id)` plus
`CHECK (information_ratio IS NULL OR benchmark_security_id IS NOT NULL)`. For a blended or
non-security benchmark, a `benchmark_returns_daily(benchmark_key, trade_date, daily_return)` table
would be the more honest home.

### S5 — `inputs_as_of` floats free of the window

```sql
--  window_start | window_end | inputs_as_of           | note
--  2026-01-05   | 2026-01-16 | 2020-01-01 00:00:00+00 | inputs predate the window by 6 years
```

`inputs_as_of` is `NOT NULL` and semantically meaningless without a relation to the window it
describes. `CHECK (inputs_as_of >= (window_end::timestamp AT TIME ZONE 'UTC'))` — same immutable
idiom as 003 — makes it a real reproducibility anchor.

### S6 — No market calendar

`portfolio_returns_daily` is keyed on `(portfolio_id, trade_date)` with no reference to a trading-day
calendar. A gap is therefore indistinguishable from a holiday, and `n_observations` silently absorbs
the difference — which propagates straight into the annualisation factor. A
`market_calendar (trade_date PRIMARY KEY, is_trading_day, session_open, session_close)` makes "the
marking job missed three days" a detectable condition rather than a slightly better Sharpe.

### N1 — `annualized_return` overflows on short windows

```sql
INSERT INTO evaluation_runs (..., annualized_return, ...) VALUES (..., 12345.6789, ...);
-- ERROR: numeric field overflow. A field with precision 12, scale 8 must round to
--        an absolute value less than 10^4.
-- (a 2-day +10% run annualises to 164,238.77)
```

It fails loud, which is the right default — but as a `numeric field overflow` rather than "you
annualised a two-day window". Widen to `NUMERIC(18,8)` and gate `annualized_return` on a minimum n so
the real error surfaces as the real error.

---

## What is missing for the framework to work end-to-end

Ordered by how much each blocks.

| # | Missing | Blocks |
|---|---|---|
| 1 | **Holdings** — `paper_portfolio_positions` (shares, entry/exit price+date, exit reason, realized P&L) and a `cash` balance | §3.1 per-position lessons; §3.3 auditability; the ≥10% cash floor; Phase 5's outcome-on-close record |
| 2 | **Guardrail events** — `guardrail_events` (rule, threshold, observed, action, override, inputs) | §5's `w_breach` term; Phase 5 observability; the ~$4k "never silently block" rule |
| 3 | **Rates** — `risk_free_rates` (point-in-time, with `known_at`) + rf/MAR/periodicity columns on `evaluation_runs` | Sharpe and Sortino being reproducible numbers rather than snapshots of a config file |
| 4 | **Judgment → outcome link** — `chosen_proposal_id`, `resulting_portfolio_id`, and a debate↔real-account bridge | §3.2 entirely |
| 5 | **Benchmark series** — a benchmark FK plus a guaranteed-aligned return series | §2's information ratio vs SPY; Phase 4's "beat buy-and-hold SPY" question |
| 6 | **Orders / fills** — a broker-truth order and fill log with unique client order IDs | §3.1 ("after trades execute"); Bar §7.2's [P0] immutable order audit log and broker reconciliation. Nothing in 001–004 records that a trade happened |
| 7 | **Walk-forward labelling** — `split`, `experiment_id`, `fold_index` | §3.5's train/test boundary; distinguishing a fitted score from an honest one |
| 8 | **Agent-level composite portfolios** — a standing portfolio per persona, rebalanced across debates | §3.3's persona-level Sharpe; §3.4's blind-vs-persona comparison being like-for-like |
| 9 | **Debate input provenance** — `debate_inputs(debate_id, source_table, source_row_id, row_as_of)` | Making the leakage audit "a query rather than an excavation", as the header claims |
| 10 | **Market calendar** | Distinguishing a missing mark from a market holiday |
| 11 | **Minimum-n policy** — `min_n_for_ranking` + a generated `is_rankable` | §2's "refuse to rank below a minimum n" |

One further gap is upstream and worth naming because it will surprise someone: **`price_bars_daily.adj_close`
is mutable by design** (002: "provider backfills adjustments", with an `updated_at` trigger). Returns
must be computed from adjusted prices. So when an adjustment lands, every previously-written
`daily_return` and every `evaluation_runs` row computed from it becomes inconsistent with the price
table, and the old `adj_close` is unrecoverable — there is no adjustment history. `inputs_as_of`
records *when* but not *what*. The append-only guarantee on `evaluation_runs` is therefore weaker than
it appears: the rows are immutable, but they are no longer reproducible from the current database.
Either version the adjustment factors (a `corporate_actions` / `price_adjustments` table) or record
the adjustment-generation id on `portfolio_returns_daily`.

---

## Coordination observations

- **Boundary with the SQL-validity reviewer.** I have deliberately not commented on naming, index
  shape, or partition mechanics beyond N7/N8. Where we may overlap: the composite-FK remedies in S1/S2
  add unique indexes, and the constraint triggers in B3/B4 add DDL that reviewer will want to check
  for lock behaviour. None of it runs against a populated table today.
- **`agent_proposal_positions`' denominator note is a live cross-project risk.** The comment states
  weights are percent-of-account-value and flags that the dashboard currently shows share-of-equity
  (issue #21). Two denominators for "25% per name" is exactly the kind of thing that produces a
  charter breach nobody sees. Whoever owns the dashboard should close #21 *before* proposals start
  being written, not after — once counterfactuals are marked under one convention and displayed under
  another, the discrepancy becomes archaeology.
- **`DATA_INVENTORY.md` constrains the framework's highest-value claim more than the schema does.**
  §2 rule 2 says the long track records come from *generating history via the backtest* — marking
  each persona's counterfactual across the full price history. The price side supports that (5 years,
  full universe, four regimes). The proposal side does not: with **4 days of fundamentals**, no
  fundamentals-driven persona can have its historical proposals reconstructed point-in-time, so there
  is nothing to mark. Today the schema can only accumulate observations *forward*, at roughly one
  debate's worth per cycle — which is precisely the low-n regime §2 warns produces "noise wearing a
  decimal point". This is not a defect in 004; it is a reason the minimum-n machinery in B6 matters
  sooner than it looks, and a further argument for the FMP purchase.
- **`004_evaluation.up.sql` omits the `-- migrate: non-destructive` header** that 001–003 carry. Per
  ADR-002 that directive is now vestigial (destructiveness comes from the filename), so 004 is
  *correct* and 001–003 are the stale ones. Worth a one-line cleanup pass on 001–003 so nobody
  reintroduces a parser for it.
- **The down migration is sound.** Drop order respects every FK (`judgments` before `debates`,
  `paper_portfolios` before `agent_proposals`), it correctly leaves 001's `set_updated_at()` alone,
  and it does not use `CASCADE` — so a future migration that adds an FK into these tables will make
  the down fail loudly rather than silently widening its blast radius. That is the right trade.

---

*Demonstration queries were run against `rh-db` (PostgreSQL 16, migrations 001–004) inside a
transaction that was rolled back. Post-run verification: all nine evaluation tables at 0 rows,
`securities`/`data_sources` at 0 rows, no leftover test constraints, `schema_migrations` = 001, 002,
003, 004. No container, volume, or `data/market/` content was touched.*
