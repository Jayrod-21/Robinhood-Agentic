# 3b. Robinhood Agentic Trader

## Description
A live, fully-agentic equities trading loop on a small ($100) Robinhood "Agentic" cash account.
The in-session Claude agent is the intelligence layer (no paid LLM API tokens), grounded in Cary
Wasden's fundamentals-first methodology. Aggressive by design — concentration, not leverage or
day-trading churn, is the source of aggression. This is a self-contained sibling of
`3a. SpecialSprinkleSauce` (Wasden Watch), which it references for the Wasden framework, screening
spec, and governance rules (see `reference/SOURCES.md`).

## Relationship to 3a
- **3a (SpecialSprinkleSauce):** the team's governed, paper-first Wasden Watch system. Shared repo
  (`JoeWhiteJr/SpecialSprinkleSauce`). Source of truth for the Wasden *thinking*.
- **3b (this):** the live, aggressive agentic loop. A separate git history so the daily journal
  churn and this account's fast-moving strategy stay out of the shared, governed 3a repo — a
  separation of concerns between two codebases, not between people.

## Account
Robinhood "Agentic" `••••4025` (cash, `agentic_allowed: true`) — the ONLY account the agent may
trade. Started June 3, 2026 with $100.00, all cash.

## Tech Stack
Python (yfinance for free fundamentals/prices) | Robinhood MCP (quotes + execution) | the in-session
Claude agent as the Wasden intelligence layer | Markdown-file journal as the self-learning memory.

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
- [x] Daily-scan: yfinance adapter → Sprinkle Sauce screen → ranked candidate report. `python -m src.daily_scan`
- [x] Forward layer: top-down pass + per-name forward theses (`THESIS_FRAMEWORK.md` + `THESES.md`).
- [x] Robinhood execution: review → place → journal. **First full slate deployed 2026-06-03** (7 positions).
- [x] Append-only log archive (`logs/`) — sessions, debates, trades.
- [x] **Dockerized live dashboard** (`backend/` FastAPI + `frontend/` Next.js, `docker-compose.yml`):
  real account monitor (bridge snapshot + live yfinance P&L), Sprinkle Sauce scan, and a live
  bull/bear + 10-agent jury debate engine. Runs on freshly-picked random ports (`bin/pick_ports.sh`);
  `bin/up.sh` builds, starts, and launches the refresh daemon. See `docs/DASHBOARD.md`.
- [x] Event store seed (`logs/events.jsonl`) — each finished debate appends a typed event.
- [x] Refresh bridge: in-dashboard button → `bin/refresh_daemon.sh` (host) → `claude` + Robinhood
  MCP → rewrites `data/account_snapshot.json`. No stored credentials.
- [x] **Ubuntu server deployment** (`deploy/` + `SERVER_DEPLOY.md`): always-on prod stack (Caddy
  single-origin + basic auth, prod Next.js build, `restart: always`), Cloudflare Tunnel, systemd
  boot unit, and a **twice-daily cron cycle** (market open + close, `TZ=America/New_York`) that
  refreshes the snapshot (headless `claude` + MCP, no wt.exe), scans the universe, debates each
  position, and writes `logs/reports/<date>-<phase>.md`. Job: `python -m app.jobs.cycle open|close`.
- [ ] Position monitoring + stop/discipline automation in the dashboard.
- [ ] Outcome logging on position close → lesson capture (self-learning loop).
