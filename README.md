# Robinhood Agentic

A live, fundamentals-first agentic trading loop — started on a small Robinhood cash account, now
running against an Alpaca paper account (see the current-state note below) — and, underneath it, the
measurement layer needed to tell whether any of it is actually working.

> **The project is not done when it makes money. It is done when a good decision and a lucky one
> are distinguishable.** Everything below follows from that.

Private repo. Co-owned — see [Ownership](#ownership).

> **Current state, 2026-08-17.** The account of record is now an **Alpaca paper account**
> (`••••I1PN`, $100,000 cash, 0 positions, margin multiplier 1 so buying power == equity), not the
> Robinhood account this file otherwise describes. `/api/account` reads Alpaca live
> (`backend/app/services/broker.py` → `src/alpaca.py`), verified against the live API.
> `ALPACA_BASE_URL` is the one variable separating paper from live trading; paper is the default.
> When Alpaca is configured but unreachable, the dashboard refuses rather than falling back to the
> Robinhood snapshot file — serving another broker's months-old holdings during an outage would show
> positions the operator does not have. The Robinhood MCP snapshot path (`data/account_snapshot.json`,
> `bin/refresh_daemon.sh`, the twice-daily cycle) still exists, is unchanged, and is still used —
> as the legacy fallback when Alpaca credentials are absent, and unconditionally by the scheduled
> cycle described below. Market data (prices, fundamentals) now comes from FMP; yfinance was removed
> from everything the dashboard ships and survives only in the corporate-actions/delistings loader
> image (`db/load_corporate_actions.py`, `db/load_delistings.py`). Everything below this note that
> describes a *past* decision or dated event keeps its original Robinhood wording on purpose — see
> `PROJECT_HISTORY.md` for the full account.

---

## What's actually here

Two things share this repository, and they are at very different stages.

**1. The live trading loop.** A real brokerage cash account — Alpaca paper as of 2026-08-17, see the
current-state note above (it started, and for most of its history has been, a Robinhood account). A
Claude session running on the host machine acts as the intelligence layer: it runs a fundamentals
screen, holds a structured bull/bear debate with a ten-agent jury, and proposes orders. **A human
owner confirms every order before it places.** There is no code path anywhere in this repository that
can place an order unattended, and that absence is deliberate.

**2. The measurement platform.** Most recent engineering effort. Trading returns are easy to
generate and famously easy to fool yourself about — split-adjust a price series wrong and a 10-for-1
split looks like a 900% gain; miss that a ticker was recycled to a different company and you book a
+1,396% "single day" return that never happened. Both of those were real bugs found and fixed here.

The platform is a Postgres database holding five years of market data, plus an evaluation framework
that scores decisions on risk-adjusted terms (Sharpe and Sortino) rather than raw return.

## Status

| Track | State |
|---|---|
| Live trading loop | **Dormant.** Last cycle 2026-06-16. Positions are held; nothing is being monitored automatically. |
| Data foundation (Phase A) | **Functional, finishing review.** 12.8M daily bars loaded and split-adjusted. |
| Evaluation engine (Phase B) | Not started. Every metric in the system waits on it. |
| Dashboard | **Running**, read-only, bound to loopback. |

Live database, as loaded:

| Table | Rows | Notes |
|---|---|---|
| `price_bars_daily` | 12,840,439 | 1,241 sessions × 19,713 securities, 2020-10-02 → 2025-10-02 |
| `corporate_actions` | 46,934 | 43,045 dividends + 3,889 splits |
| `market_calendar` | 2,557 | 1,759 trading days, derived from exchange **rules** not data coverage |
| `risk_free_rates` | 29,358 | Needed before any Sharpe ratio can exist |
| `securities` | 19,713 | 8,033 marked delisted — survivorship bias is tracked, not ignored |

Migrations 001–007 applied; 008 written and pending.

## Known limits

Stated up front, because overstating what the data supports is the failure mode this project is
built to avoid:

- **Dividend coverage is ~5% of securities**, so computed returns are price-only. The schema
  physically refuses to store a price return labelled as a total return.
- **The stored daily close is the 15:59 bar**, not the official closing auction print — that can't
  be recovered from minute aggregates. Worst measured deviation 95.7 bps; volume runs ~15% low.
- **15 trading days of December 2024 are missing.** The source archive files are corrupt on the
  drive itself, so re-copying doesn't help.
- **Fundamentals history is four days of a Bloomberg export** — enough to validate the screen
  against real values, nowhere near enough to backtest.
- **Nothing in the application reads the database yet.** The limits above are documented before
  anything depends on them.

## Quick start

Requires Docker, Python 3.12+, and Node 20+.

```bash
# Database — internal-only network, no host port (see docs/adr/ADR-001)
bash bin/db_up.sh
bash bin/db_migrate.sh up
bash bin/db_psql.sh -c "select count(*) from price_bars_daily;"

# Dashboard — picks free ports, builds both images, prints the URLs
bash bin/up.sh

# Tests — the authoritative gate, six suites in CI-pinned containers
bash bin/local_test.sh
```

The gate runs inside pinned containers on purpose: the host may have a newer Python than CI, and a
green run against host Python is a claim about your machine, not about what ships. See `TESTS.md`.

## Layout

```
backend/     FastAPI dashboard API + the debate engine
frontend/    Next.js dashboard (Portfolio, Scan, Pipeline, Debate)
src/         The screen and data adapters
db/          Migrations, loaders, verification tools
bin/         Operational scripts — database, dashboard, tests, refresh bridge
deploy/      Production compose, Caddy, tunnel config (not yet deployed)
docs/        Charter, plan, evaluation framework, ADRs, review reports
logs/        Append-only historical record — sessions, debates, trades
```

## Key documents

Read in this order:

1. **[`PROJECT_PLAN.md`](PROJECT_PLAN.md)** — what "done" means, phases A–G with checkable exit
   criteria, and the critical path.
2. **[`docs/AGENTIC_ROBINHOOD_v1.md`](docs/AGENTIC_ROBINHOOD_v1.md)** — the operating charter:
   mission, risk rules, position limits, and who may approve what.
3. **[`docs/EVALUATION_FRAMEWORK.md`](docs/EVALUATION_FRAMEWORK.md)** — how decisions get scored,
   including the blind control agent and the leakage rules.
4. **[`docs/DATA_INVENTORY.md`](docs/DATA_INVENTORY.md)** — what data exists and what it supports.
5. **[`SENIOR_ENGINEER_BAR.md`](SENIOR_ENGINEER_BAR.md)** — the quality contract every change is
   reviewed against. §7.2 is binding on the order path.
6. **[`docs/adr/`](docs/adr/)** — decisions with reasoning: network isolation, destructiveness.

## How work happens here

- **Every deliverable goes through independent review** before it is trusted. Reviewers who did not
  write the code look for blockers; a separate pass fixes them; a third pass verifies the fixes hold.
  Reports live in `docs/fixpass/`. This has caught 20+ blockers so far.
- **The recurring defect is a stored value or a written claim that means something other than its
  name says.** It has appeared at every review stage. Assume it is present in anything new — that is
  why claims in this repo are expected to carry their evidence.
- **Migrations are never edited once applied.** A checksum guard enforces it. Corrections go in a new
  migration.
- **Guardrails must be tunable, observable, and overridable — never a silent block.** A guardrail
  that blocks a valid action must announce that it did, and why.

## Authentication

Per-operator authentication (Argon2id password + TOTP, `__Host-`-prefixed sessions, single-use
recovery codes, 5-strike/15-minute lockout) is built and migrated into `rh-db` — see
`docs/AUTH_THREAT_MODEL.md` for the full threat model, its status banner for exactly what has been
verified against the tree, and §10 for the reconciled test-plan status. **Cut over in production on
2026-08-14**, after a real browser login was completed end-to-end against the deployed stack; the
Caddy basic-auth gate that preceded it has been removed. §5.14 of the threat model records what
replaced it and what that cost — read it before changing the rate limits or the proxy chain.

**Account creation is CLI-only by design.** There is no signup route and no self-service password
reset. New operators are seeded with `bin/db_manage_operator.sh seed --email …`; the same script
disables, unlocks, and resets accounts (`bin/manage_operator.py`). This is a deliberate invariant
(`docs/AUTH_THREAT_MODEL.md` §11), not a missing feature — never add a signup endpoint.

## Ownership

Co-owned. Decision rights, approval authority, and the autonomy ladder are defined in the charter
(`docs/AGENTIC_ROBINHOOD_v1.md`) rather than attached to any individual.

Records in `logs/` dated before 2026-08-13 were written during a single-operator build phase and
name one person as approver. They are deliberately not rewritten — see the note in
`logs/README.md`.

## A note on the account

The trading account was small by design; concentration, not leverage or day-trading churn, was the
source of aggression on the original $100 Robinhood account. The interesting asset was never the
balance — it was order-placement authority against a live brokerage account, plus a host session
authenticated to that broker. The security posture is built around protecting that, and
`docs/SECURITY_FINDINGS_2026-07-27.md` catalogues what is still open.

**As of 2026-08-17** the account of record is an Alpaca **paper** account ($100,000 simulated cash) —
see the current-state note near the top of this file. No real capital is presently at stake through
it. The reasoning above still holds for whichever account is live at a given time; it will apply
again, unchanged, once the account of record is a funded brokerage account.
