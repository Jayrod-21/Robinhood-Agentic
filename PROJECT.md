# 3b. Robinhood Agentic Trader

## Description
A live, fully-agentic equities trading loop, started on a small ($100) Robinhood "Agentic" cash
account — see [Current account state](#current-account-state-2026-08-17) below for what's live today.
The in-session Claude agent is the intelligence layer (no paid LLM API tokens), grounded in Cary
Wasden's fundamentals-first methodology. Aggressive by design — concentration, not leverage or
day-trading churn, is the source of aggression. This is a self-contained sibling of
`3a. SpecialSprinkleSauce` (Wasden Watch), which it references for the Wasden framework, screening
spec, and governance rules (see `reference/SOURCES.md`).

## Current account state (2026-08-17)
The account of record is now an **Alpaca paper account** (`••••I1PN`, $100,000 cash, 0 positions,
margin multiplier 1). `/api/account` reads Alpaca live (`backend/app/services/broker.py` →
`src/alpaca.py`); `ALPACA_BASE_URL` is the one variable separating paper from live, and paper is the
default. If Alpaca is configured but unreachable, the dashboard refuses rather than falling back to
the Robinhood snapshot. The Robinhood MCP snapshot path (`data/account_snapshot.json`,
`bin/refresh_daemon.sh`) is unchanged and still runs — as the legacy fallback when Alpaca credentials
are absent, and unconditionally as part of the twice-daily scheduled cycle (neither script was
touched by the Alpaca work). Market data (prices, fundamentals) now comes from FMP; yfinance was
removed from everything the dashboard ships and survives only in the corporate-actions/delistings
loader image. The `••••4025` account and dates below describe the project's actual history and are
unchanged.

## Relationship to 3a
- **3a (SpecialSprinkleSauce):** the team's governed, paper-first Wasden Watch system. Shared repo
  (`JoeWhiteJr/SpecialSprinkleSauce`). Source of truth for the Wasden *thinking*.
- **3b (this):** the live, aggressive agentic loop. A separate git history so the daily journal
  churn and this account's fast-moving strategy stay out of the shared, governed 3a repo — a
  separation of concerns between two codebases, not between people.

## Accounts
**Account of record: Alpaca paper `••••I1PN`, ~$100,000** (since 2026-08-17). More Alpaca paper
accounts are planned for testing; `GET /api/accounts` is the live registry, populated from
`ALPACA_ACCOUNT_<N>_*` environment variables. Each account reconciles against **its own** slate
(`docs/SLATE.md` for account 1, `docs/slates/account-N.md` for the rest) or against none —
one account's targets are never applied to another's holdings.

*Historical:* Robinhood "Agentic" `••••4025` (cash, `agentic_allowed: true`) was the only account
the agent could trade. Started June 3, 2026 with $100.00, all cash. Superseded 2026-08-17 — see
[Current account state](#current-account-state-2026-08-17) above.

## Tech Stack
Python | FMP (fundamentals/prices — replaced yfinance in everything the dashboard ships; yfinance
survives only in the corporate-actions/delistings loader image) | Alpaca (paper account reads,
`src/alpaca.py`, `ALPACA_BASE_URL` selects paper vs. live) | Robinhood MCP (quotes + execution;
still used by the twice-daily refresh cycle and as the account-read fallback when Alpaca credentials
are absent) | the in-session Claude agent as the Wasden intelligence layer | Markdown-file journal as
the self-learning memory.

## Status
Active — bootstrap phase (a human owner confirms every order before it places).

## Key Docs
- `PROJECT_PLAN.md` — **the living plan**: current problems (P1–P10), build phases, testing
  strategy, and what's blocked on an owner decision. Start here to pick up work.
- `docs/AGENTIC_ROBINHOOD_v1.md` — operating charter (mission, model, risk rules, autonomy).
- `docs/agentic_journal.md` — live trading journal / self-learning substrate (running ledger).
- `docs/THESIS_FRAMEWORK.md` + `docs/THESES.md` — the forward-thinking layer (top-down pass + per-name forward theses).
- `docs/SLATE.md` — current target allocation.
- `reference/SOURCES.md` — pointers to the canonical Wasden material in 3a.
- `logs/` — append-only deep archive: `sessions/` (narratives), `debates/` (multi-agent records), `trades/` (execution logs). Seed of the future 24/7 app's event store — see `logs/README.md`.

## Roadmap (incremental)
- [x] Daily-scan: FMP adapter (originally yfinance, replaced — see `src/data.py`'s module docstring)
  → Sprinkle Sauce screen → ranked candidate report. `python -m src.daily_scan`
- [x] Forward layer: top-down pass + per-name forward theses (`THESIS_FRAMEWORK.md` + `THESES.md`).
- [x] Robinhood execution: review → place → journal. **First full slate deployed 2026-06-03** (7 positions).
- [x] Append-only log archive (`logs/`) — sessions, debates, trades.
- [x] **Dockerized live dashboard** (`backend/` FastAPI + `frontend/` Next.js, `docker-compose.yml`):
  real account monitor (bridge snapshot + live P&L — yfinance originally, replaced by FMP; as of
  2026-08-17 the account itself is read from Alpaca directly when configured, see
  [Current account state](#current-account-state-2026-08-17)), Sprinkle Sauce scan, and a live
  bull/bear + 10-agent jury debate engine. Runs on freshly-picked random ports (`bin/pick_ports.sh`);
  `bin/up.sh` builds, starts, and launches the refresh daemon. See `docs/DASHBOARD.md`.
- [x] Event store seed (`logs/events.jsonl`) — each finished debate appends a typed event.
- [x] Refresh bridge: in-dashboard button → `bin/refresh_daemon.sh` (host) → `claude` + Robinhood
  MCP → rewrites `data/account_snapshot.json`. No stored credentials. Still in place, unchanged; as
  of 2026-08-17 this is the legacy fallback path — `/api/account` reads Alpaca directly when
  configured, and only falls back to this file when Alpaca credentials are absent (see
  [Current account state](#current-account-state-2026-08-17)).
- [x] **Ubuntu server deployment** (`deploy/` + `SERVER_DEPLOY.md`): always-on prod stack (Caddy
  single-origin + basic auth, prod Next.js build, `restart: always`), Cloudflare Tunnel, systemd
  boot unit, and a **twice-daily cron cycle** (market open + close, `TZ=America/New_York`) that
  refreshes the snapshot (headless `claude` + MCP, no wt.exe), scans the universe, debates each
  position, and writes `logs/reports/<date>-<phase>.md`. Job: `python -m app.jobs.cycle open|close`.
- [ ] Position monitoring + stop/discipline automation in the dashboard.
- [ ] Outcome logging on position close → lesson capture (self-learning loop).
