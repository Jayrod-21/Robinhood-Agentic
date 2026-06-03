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
- `docs/agentic_journal.md` — live trading journal / self-learning substrate.
- `reference/SOURCES.md` — pointers to the canonical Wasden material in 3a.

## Roadmap (incremental)
- [ ] Daily-scan: yfinance adapter → Sprinkle Sauce screen → Wasden lens → candidate write-up (no trades).
- [ ] Risk + pre-trade validation tuned for $100.
- [ ] Robinhood execution glue (review → confirm → place → journal).
- [ ] Outcome logging + lesson capture loop (self-learning).
- [ ] Scheduled daily runner (`/schedule`) once the loop is trusted.
