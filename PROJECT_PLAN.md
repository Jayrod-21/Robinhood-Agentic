# 3b — Project Plan

**Rewritten 2026-07-28.** The previous version's phases predated the evaluation framework, the data
landing, and the reordering around infrastructure-then-UI. This one starts from a stated end state
and works backwards.

---

## 1. What "done" means

Concretely, so it can be checked rather than felt:

> Every trading day, on M, the system pulls the account, refreshes market data, runs the Sprinkle
> Sauce screen over a real universe, holds a debate whose agents can read the database and the
> knowledge base, scores every proposal — winning and losing — against a risk-adjusted reward, writes
> the outcome back where the next debate will read it, and surfaces all of it in a UI the owners use to
> see the dilemma, the evidence, and each agent's track record. Orders are proposed by the system and
> confirmed by a human. Nothing acts on a stale or unreconciled book.

Two properties matter more than any feature:

- **It can tell you when it is wrong.** Reconciliation against the broker, sample sizes beside every
  ratio, guardrails that announce what they blocked and why.
- **Its numbers survive scrutiny.** No lookahead, no survivorship bias, no unreproducible metric.

The system is *not* done when it makes money. It is done when a good decision and a lucky one are
distinguishable.

---

## 2. Where we actually are

| Built and verified | Status |
|---|---|
| Postgres on M — isolated, hardened, digest-pinned | ✅ |
| Migration runner — filename-marked destructive gate, server-enforced transaction integrity | ✅ (5 review rounds) |
| Schema 001–004 — securities, provenance, partitioned bars, point-in-time fundamentals, 13 evaluation tables | ✅ |
| 5-year minute-bar archive on disk, verified byte-exact | ✅ 1,256 files / 24 GB |
| Minute-bar loader — streaming, resumable, partition-aware | ✅ 6 files loaded as proof |
| Robinhood MCP connected; account readable | ✅ |
| Dashboard prototype (Portfolio / Scan / Pipeline / Debate) | ⚠️ exists, unpolished |
| 204 tests, CI green | ✅ |

*(2026-08-17 update: the dashboard's account of record is now an Alpaca paper account, read
directly via `src/alpaca.py`; the "Robinhood MCP connected" row above is unchanged and still
describes the refresh-daemon/scheduled-cycle path, which still runs and still serves as the
fallback when Alpaca credentials are absent — see `SECURITY.md` §0.)*

**The honest summary: we have a trustworthy place to put data, and almost nothing that produces it.**

---

## 3. What we were missing

This is the part the old plan got wrong. These are components nothing in the repo produces, several
of which other components already assume exist.

### 3.1 Silent-correctness gaps — highest risk, because they produce plausible wrong numbers

| Gap | Why it bites |
|---|---|
| **Corporate actions / splits** | `price_bars_daily.adj_close` exists, its comment says "returns use this", 004's marking job was originally specified as `Σ shares × adj_close + cash` — and **nothing populates it**. NVDA split 10:1 in June 2024, inside our window. An unadjusted return across a split is wrong by 10× and looks like a triumph. *(Update, Phase A fix-pass: 005-007 populate the split adjustment, and the marking formula was corrected — marking is `Σ shares × RAW close + cash` with lot share counts split-adjusted on ex-date; `Σ shares × adj_close` mixes share bases and mis-marks any lot held across a split. See migration 007's comments.)* |
| **Market calendar** | Table exists, empty. Without it, "trading days" is guesswork: return series get holes on holidays, and `n_observations` counts calendar days instead of sessions. |
| **Risk-free rates** | Table exists, empty. Every Sharpe needs one. Until it is populated, no ratio can be computed at all. |
| **Benchmark series** | Information ratio and "did we beat SPY" both need it. Nothing fetches it. |
| **Delisting detection** | `securities.delisted_at` exists and the loader deliberately never sets it. Until something does, the universe silently accumulates survivorship bias. |

### 3.2 Compute that doesn't exist yet

| Gap | Note |
|---|---|
| **The marking job** | Values every paper portfolio daily. **The entire evaluation layer depends on it and no plan listed it.** Without it `portfolio_returns_daily` stays empty and every metric is uncomputable. |
| **Metric computation** | The tables hold Sharpe/Sortino; nothing calculates them. |
| **Feature store** | Between raw bars and any model. Must enforce as-of correctness or leakage returns. |
| **The real screen** | `src/screen.py` is a thin yfinance approximation. 3a's `screening_engine.py` + `piotroski.py` is the intended brain. |
| **Backtest engine** | Port from 3a. Needs the calendar, adjusted prices, and point-in-time fundamentals to be honest. |
| **Reconciliation** | #22. The book already drifted once, undetected, for seven weeks. |

### 3.3 Agent and app work

Debate DB + knowledge-base access (#25) · outcome logging (#4) · order path with guardrails ·
the UI itself, which is a workstream rather than four backlog items.

### 3.4 Operations

Scheduler (what runs when, and what happens when a run fails) · alerting · DB backup and restore ·
deployment as purple/yellow behind the tunnel · the Phase-1 security baseline.

---

## 4. Phases

Each has an entry condition, an exit criterion that can be checked, and a reason for its position.
**Phases A–C are the gate on buying FMP Premium.**

### Phase A — Data foundation *(in progress)*

*Everything downstream reads this. Nothing else can be trusted until it is right.*

1. **Daily bars** derived from the minute archive — ~11M rows, minutes to load, versus 25 hours for
   the full minute set. The evaluation framework computes on daily returns, so this unblocks the most
   per hour spent.
2. **Corporate actions** — source split/dividend history and populate `adj_close`. Until this exists,
   any computed return is provisionally wrong.
3. **Market calendar** and **risk-free rates** loaders — small, and everything numeric waits on them.
4. **Benchmark series** (SPY, QQQ) as securities with daily bars.
5. Minute bars, later and in the background, when intraday features are actually needed.

**Exit:** a return series for any security over any window is computable, split-adjusted, on real
trading days — and a test proves a known split (NVDA 2024-06) produces a continuous series.

### Phase B — The evaluation engine

*Turns the 13 tables from storage into a working loop.*

1. **The marking job** — value every paper portfolio daily from holdings × adjusted close + cash.
2. **Metric computation** — Sharpe, Sortino, drawdown, hit rate, information ratio, with `n`,
   convention, and reward weights recorded per run.
3. **Outcome logging** (#4) — entry thesis → exit → realized score → lesson, into the knowledge base.
4. **Backfill** — replay historical proposals to give the counterfactual records enough observations
   to be readable.

**Exit:** a paper portfolio with a real proposal produces a Sharpe and a Sortino you can recompute by
hand from the stored inputs, and a judge can query the realized score of its own prior ruling.

### Phase C — The debate can reason, and the UI is usable

*The two workstreams you named. They run in parallel.*

**C1 — agents:** DB-backed context (fundamentals filtered on `known_at`, price history, positions,
prior decisions with scores) · knowledge-base retrieval · an analysis surface so agents request
computed statistics rather than eyeballing prompt text · third-party text delimited as data, never
instructions.

**C2 — UI:** the Weight-denominator bug (#21) · debate detail with full reasoning (#26) · full
fundamentals on Scan (#27) · pipeline history (#28) · portfolio chart with benchmarks (#29) ·
agent track-record views showing `n` beside every ratio.

**Exit:** a debate runs entirely from the database with no live scraping, and an owner can read a
decision, its evidence, and each agent's record without opening psql.

### → FMP Premium purchase happens here

With A–C done there is a pipeline ready to consume real fundamentals history. Buying earlier means
paying while the consumer is still being written.

### Phase D — The real screen and the backtest

Port 3a's screening engine and Piotroski onto FMP's full statements · port the backtesting engine ·
**a dedicated lookahead test** — the single test that decides whether any backtest result means
anything.

**Exit:** the screen reproduces 3a's ranking on shared tickers, and a ≥5-year walk-forward backtest
reports return, drawdown, and Sharpe against SPY with no lookahead.

### Phase E — Risk, execution, reconciliation

Charter rules as code (≤25%/name, 10–20% cash, exit-before-entry, no averaging down) — tunable,
observable, overridable, never a silent block · stops and targets · broker reconciliation with
halt-on-mismatch · the order path with per-order step-up auth and an idempotency key · kill switch.

**Exit:** a simulated breach of each rule produces the right decision *and* a legible explanation; a
valid trade is never blocked; a deliberate book/broker mismatch halts the cycle.

### Phase F — Hosting and operations

Purple/yellow on M behind the Cloudflare Tunnel · the Phase-1 security baseline (#11 CSRF first) ·
scheduler · alerting · DB backup, restore, and a tested restore drill.

**Exit:** reachable at its subdomain, origin not directly reachable, a scheduled cycle runs unattended
and alerts on failure, and a restore from backup has actually been performed once.

### Phase G — ML

Feature store with enforced as-of correctness · GBM baseline first · then the neural work, measured
against that baseline and against the blind agent.

**Exit:** a model beats the GBM baseline *and* the blind agent on risk-adjusted out-of-sample
performance — or is honestly reported as not doing so.

---

## 5. Critical path

```
daily bars ─┐
splits ─────┼─→ marking job ─→ metrics ─→ counterfactual records ─→ agents can reason
calendar ───┤                                                              │
rf rates ───┘                                                              ↓
                                                                    UI shows it
```

**Splits and the marking job are the two nobody had listed, and everything numeric sits behind
them.** The screen, the backtest, and the ML phases are all downstream of a correct return series.

---

## 6. Standing constraints

| Constraint | Detail |
|---|---|
| **UI + infra before FMP** | Phases A–C gate the purchase. |
| **No live trading meanwhile** | Reconciliation is a record-keeping task, not a trading one, until Phase E. |
| **`/fixpass` on everything created** | All four phases, every time. It has caught 20+ blockers so far, most of them claims that didn't survive testing. |
| **`SENIOR_ENGINEER_BAR.md`** | §7.2 binds the order path: `Decimal` not float, kill switch, unique client order id, reconcile before resubmit. |
| **Verify a host port before creating a container** | M runs 9b alongside; on collision take a new port. |
| **Guardrails must not silently block** | The ~$4k lesson. Tunable, observable, overridable. |
| **Free FMP tier meanwhile** | 250 calls/day. Cache to Postgres; fixture-back tests. |

---

## 7. Risks worth stating

**Point-in-time fundamentals may be unobtainable.** Robinhood's financials carry `period_end_date`
but no filing date, and FMP's historicals may be as-restated rather than as-first-reported. If
neither supplies a filing date, no fundamentals backtest can honestly claim point-in-time
correctness — the ceiling on what we can assert would drop, and it is better to know that before the
purchase than after. **Ask FMP directly.**

**Five years is one sample.** It covers four regimes, which is enough to evaluate honestly and not
enough to prove an edge survives every market.

**The 20–40% monthly target is extraordinary.** The reward function stops the system reaching for it
recklessly; it cannot make it achievable. If the honest long-run answer is that the target is
unreachable without ruin risk, this framework's job is to say so legibly.

**Scope.** Phases D–G are each substantial. The plan is deliberately ordered so that stopping after
any phase leaves something coherent rather than half a system.
