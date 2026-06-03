# TESTS.md — 3b. Robinhood Agentic Trader

Consumed by `/testcheck`. Each suite lists its command and pass criteria.

## Suites

### 1. Screen unit tests
- **Command:** `python3 -m pytest tests/ -q`
- **Pass criteria:** all tests pass (currently 18). Covers the Sprinkle Sauce tier logic
  (liquidity floor, PEG/FCF/Piotroski gates, proportional single-snapshot scoring, ranking)
  and the yfinance `.info` field mapping (FCF-yield computation, PEG fallback, NaN/missing handling).
- **Network:** none required — all pure functions on synthetic fundamentals.

## Smoke check (manual, needs network)
- **Command:** `python3 -m src.daily_scan AAPL NVDA OXY`
- **Expected:** prints a scan report with survivors ranked and rejects annotated with the failing
  tier + reason. Not part of the automated suite (depends on live yfinance data).
