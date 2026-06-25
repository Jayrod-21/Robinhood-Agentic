# TESTS.md — 3b. Robinhood Agentic Trader

Consumed by `/testcheck`. Each suite lists its command and pass criteria.

## Suites

### 1. Screen unit tests
- **Command:** `python3 -m pytest tests/ -q`
- **Pass criteria:** all tests pass (currently 18). Covers the Sprinkle Sauce tier logic
  (liquidity floor, PEG/FCF/Piotroski gates, proportional single-snapshot scoring, ranking)
  and the yfinance `.info` field mapping (FCF-yield computation, PEG fallback, NaN/missing handling).
- **Network:** none required — all pure functions on synthetic fundamentals.

### 2. Dashboard backend unit tests
- **Command:** `python3 -m pytest backend/tests/ -q`
- **Pass criteria:** all tests pass. Covers jury aggregation (6+ majority decides, a true BUY/SELL
  directional deadlock escalates while a BUY/HOLD or SELL/HOLD tie resolves to HOLD, plurality holds,
  odd juries can't deadlock), account snapshot validation, the live-marks overlay P&L/weights math +
  unpriced soft-fail, marks TTL caching, debate-record archive parsing + round-trip, the shared
  cooldown limiter (debate ↔ pipeline share one budget), the scan ticker-list cap + all-invalid
  rejection, the atomic refresh-trigger write + cooldown/pending gates, input validation, and — the
  security regression test — `get_record` rejecting path-traversal record ids.
- **Network:** none — yfinance/Anthropic calls are monkeypatched.

### 3. Frontend build
- **Command:** `cd frontend && npm install --no-audit --no-fund && npm run build`
- **Pass criteria:** `next build` compiles all routes with no type errors (/, /scan, /pipeline, /debate).

## Smoke checks (manual, need network / Docker)
- **Daily scan:** `python3 -m src.daily_scan AAPL NVDA OXY` → ranked survivors + annotated rejects (live yfinance).
- **Full stack:** `bash bin/up.sh` (needs Docker Desktop running) → open `http://localhost:$FRONTEND_PORT`:
  Portfolio shows the real account with live P&L; Refresh opens an MCP-bridge tab; Scan streams the
  screen; Pipeline/Debate run the live jury (needs `ANTHROPIC_API_KEY` in `backend/.env`).
- **Ports:** `bash bin/pick_ports.sh` twice → two distinct free ports each run, different across runs.
