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
- **3b (this):** Jared's personal, live, aggressive agentic loop. Separate git history so the daily
  journal churn and personal strategy stay out of the shared repo.

## Account
Robinhood "Agentic" `••••4025` (cash, `agentic_allowed: true`) — the ONLY account the agent may
trade. Started June 3, 2026 with $100.00, all cash.

## Tech Stack
Python (yfinance for free fundamentals/prices) | Robinhood MCP (quotes + execution) | the in-session
Claude agent as the Wasden intelligence layer | Markdown-file journal as the self-learning memory.

## Status
Active — bootstrap phase (Jared confirms every order before it places).

## Key Docs
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
- [ ] Scheduled daily pre-market scan routine (in progress).
- [ ] Local 24/7 app/script: event store (JSONL), position monitoring, stop/discipline automation, stored RH auth.
- [ ] Outcome logging on position close → lesson capture (self-learning loop).
