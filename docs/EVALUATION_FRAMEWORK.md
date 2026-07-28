# Evaluation framework — the reward signal

How 3b decides whether a decision was *good*, as distinct from whether it *made money*.

**Source:** conversation with Isaac, 2026-06-23, recorded in
`Agentic Financial Model 6_23_otter_ai_transcript.txt` (Google Drive → "Agentic Trading Model").
This document turns that conversation into a specification.

---

## 1. The problem

The system prompt targets **20–40% monthly return with ≥10% cash**. If the only reward is the return,
the optimizer's most rational move is maximum concentration — that is the only thing that plausibly
reaches 20% in a month. From the transcript:

> "if you just do the 20 to 40% it's going to do the most risky decision and put everything in the
> micron and lose all your money, because that was the only thing you could think of that would be
> the best alternative to making 20% at least. So we need some kind of way to gage if it was a good
> decision or not, **other than just win-lose**."

That is textbook reward hacking. The objective is satisfiable by a strategy nobody wants.

It also matters that a single realized outcome carries almost no information. A trade that returned
+30% may have been a reckless bet that happened to land; one that returned −5% may have been correct
and unlucky. **Outcome is not the same as decision quality**, and a learning loop that can't tell them
apart will train the wrong behavior.

## 2. The two metrics

| Metric | Definition | Penalizes |
|---|---|---|
| **Sharpe ratio** | (return − risk-free) ÷ standard deviation of returns | *All* volatility, up and down |
| **Sortino ratio** | (return − target) ÷ downside deviation | Only downside volatility |

Both are carried, not one. They disagree in a way that is informative here: Sharpe punishes a
strategy for large upside moves, which for a deliberately aggressive book is a feature being scored
as a fault. Sortino only punishes losses. **When Sortino is high but Sharpe is low, the strategy is
volatile to the upside** — exactly the profile 3b is aiming at, and precisely the case where Sharpe
alone would mislead.

### The sample-size problem — the thing most likely to go wrong

Sharpe and Sortino are **estimates**, and noisy ones. Their standard error scales roughly with
1/√n. Computed on monthly returns you would need years before the value means anything; on the
handful of trades this account has made, the number is noise wearing a decimal point.

Three rules follow, and they are not optional:

1. **Compute on daily returns and annualize.** Roughly 21× the observations per unit of calendar time
   versus monthly.
2. **Generate history from the backtest**, not only from realized trades. Each persona's counterfactual
   portfolio (§3.3) can be marked daily across the full price history, which is what makes its track
   record long enough to read.
3. **Always store `n_observations` next to every ratio, and refuse to rank below a minimum n.** A
   persona that looks brilliant over six days is reporting nothing. The UI must show n, and any
   leaderboard must suppress or grey out under-powered rows rather than silently ranking them.

Supporting metrics stored alongside, because two numbers cannot describe a return distribution:
max drawdown, hit rate, average win ÷ average loss, turnover, and — once the strategy is
benchmark-aware — information ratio versus SPY.

## 3. Where the reward is applied

Three placements, specified in the transcript. Each is a distinct loop.

### 3.1 Post-trade feedback → knowledge base

After trades execute, compute the realized return and its risk-adjusted scores, and write both into
the knowledge base as history. This is the ground-truth record everything else is measured against:
what was decided, what happened, and what it scored.

### 3.2 Judges review the after-effect of their own prior judgments

A judge, before ruling, sees the realized Sharpe/Sortino of decisions **it previously made**. From the
transcript: *"the judges will be able to look at the after effect of the previous judgment that they
made … learn off of it, and have that kind of influence the judgment that they're doing, so then they
can forward project."*

This is a per-judge calibration record. A judge that consistently backs decisions scoring poorly
risk-adjusted should discover that about itself and adjust.

### 3.3 Per-persona counterfactual track records

Every debating persona — bullish, bearish, optimistic, pessimistic, the Wasden lens, and so on — keeps
its own record of *what would have happened had its recommendation been followed*, whether or not it
won the debate.

Mechanically: each persona's proposal becomes a **paper portfolio**, marked daily against real prices.
Each therefore accrues its own return series and its own Sharpe/Sortino over a long window.

This is the highest-value piece, for two reasons. It yields *far* more observations than realized
trades alone — losing proposals are scored too, so every debate produces N data points instead of one.
And it makes persona quality measurable rather than assumed: if the pessimist has carried a better
risk-adjusted record than the optimist for a year, that is a fact about the system, not a vibe.

### 3.4 The blind agent

A control with no persona and no exposure to the return-target prompt, which simply tries to produce a
return. Scored identically to every persona.

Its purpose is to answer: **does the debate machinery beat a plain agent?** If the personas cannot
out-score the blind agent risk-adjusted, the debate is expensive theatre and should be simplified.
That is a question worth being able to answer honestly, and it needs the control to exist from the
start.

### 3.5 Data leakage — what actually defends against it

The transcript flags leakage as a concern and notes the blind agent was *thought* to help, with
uncertainty about whether it does. It does not. The blind agent controls for **persona bias**, which is
a different failure. Leakage is defeated structurally:

- **As-of-timestamp features.** Every feature carries the timestamp of the data it was computed from,
  and a feature may only use rows whose timestamp is ≤ the decision time. Enforced in the feature
  store, not by convention.
- **Walk-forward evaluation** with a hard train/test boundary. Never a random split — random splits on
  time series leak the future into the past by construction.
- **Point-in-time fundamentals.** Screening today's fundamentals against past prices is the classic
  version of this error, and it produces backtests that look extraordinary and are worthless.
- **A dedicated lookahead test** in the suite, per `PROJECT_PLAN.md` Phase 4.

Cheap to enforce up front, effectively impossible to retrofit — which is why it belongs in the schema
now rather than in the ML phase later.

## 4. Schema implications

What the above requires the database to hold. This is the part the Phase 2 schema was waiting on.

| Table | Purpose |
|---|---|
| `agents` | Persona registry — bull, bear, optimist, pessimist, Wasden lens, judges, **blind**. Versioned, since a prompt change makes it a different agent for scoring purposes. |
| `debates` | One row per debate: inputs, timestamp, participants. |
| `agent_proposals` | What each persona proposed in each debate — the target weights. The seed of every counterfactual portfolio. |
| `judgments` | Which judge ruled what, and why. Joins to the realized outcome for §3.2. |
| `paper_portfolios` | One per persona per debate (plus the real portfolio and the blind agent). |
| `portfolio_returns_daily` | Daily marked return per portfolio, real and counterfactual alike. The observation table every metric reads. |
| `evaluation_runs` | Computed metrics for a (portfolio, window): sharpe, sortino, max_dd, hit_rate, **n_observations**, window bounds, and the code version that produced them. |
| `knowledge_base_entries` | The §3.1 lessons — decision, outcome, score, what was learned. |

Two constraints that must hold at the schema level:

- **`n_observations` is NOT NULL on `evaluation_runs`.** A ratio without its sample size is not a
  usable number, and making it nullable guarantees somebody eventually reads one that is.
- **Every feature and metric row records the as-of timestamp of its inputs**, so a leakage audit is a
  query rather than an archaeology project.

## 5. Reward composition

The scalar the learning loop optimizes is a weighted composite, not a single ratio:

```
reward = w_sortino · Sortino
       + w_sharpe  · Sharpe
       − w_dd      · max_drawdown_penalty
       − w_breach  · guardrail_breach_penalty
```

Per the standing rule that risk controls be **tunable, observable, and overridable**: every weight
lives in config, every computed reward is stored with the weights that produced it, and a change of
weights is a new `evaluation_runs` row rather than a silent overwrite of history. Without that, a
tweak to `w_sharpe` rewrites the past and no comparison across time is meaningful.

The `guardrail_breach_penalty` term is what keeps the ≥10% cash floor and the ≤25% per-name limit
inside the objective rather than bolted on outside it. A strategy that hits its return target by
breaching the charter must score *worse*, not better.

## 6. What this does not do

It does not make a 20–40% monthly target achievable. Risk-adjusting the reward stops the optimizer
from reaching for that target through recklessness; it does not conjure the return. If the honest
long-run risk-adjusted answer is that the target is unreachable without unacceptable ruin risk, this
framework's job is to **say so legibly** — which is worth considerably more than a system that hides
the fact until the account is gone.
