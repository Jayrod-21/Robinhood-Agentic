# TESTS.md — 3b. Robinhood Agentic Trader

Consumed by `/testcheck`. Each suite lists its command and pass criteria.

**Setup:** run from the project root with the venv active (`python3 -m venv .venv && .venv/bin/pip
install -r backend/requirements.txt ruff`, or `make venv`). Commands below say `python3`; use
`.venv/bin/python` if the venv isn't activated — the backend suite needs `fastapi`/`pydantic`, which
are not in the system interpreter.

## Suites

### 0. Lint
- **Command:** `ruff check backend/app src db`
- **Pass criteria:** `All checks passed!`. Rule selection and line length are pinned in
  `pyproject.toml`, and CI pins the ruff version — so a lint failure means a real code change,
  not a new ruff release.

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

Suites 1 and 2 can also be run together as a bare `python3 -m pytest` from the root (87 tests) —
`pyproject.toml` sets importlib import mode so the two same-named `tests` packages don't collide.

### 4. Database migrations (needs Docker)
- **Command:** `bash bin/db_up.sh && bash bin/db_migrate.sh up`
- **Pass criteria:** all migrations apply; `bash bin/db_migrate.sh status` shows every version
  `applied` with checksum `ok`.
- **Round trip:** `bash bin/db_migrate.sh down --allow-destructive --target 000` then `up` again —
  proves each `.down.sql` truly reverses its `.up.sql` and leaves no residue.
- **Guard checks** (each must fail loudly): `down` without `--allow-destructive` → exit 1; editing an
  applied migration → `ChecksumMismatch`, exit 1; a migration containing a top-level `BEGIN`/`COMMIT`
  → rejected at discovery, exit 1; bad credentials → exit 3.
- **Network:** none beyond Docker. The database is on an internal-only network with no egress.

## Smoke checks (manual, need network / Docker)
- **Daily scan:** `python3 -m src.daily_scan AAPL NVDA OXY` → ranked survivors + annotated rejects (live yfinance).
- **Full stack:** `bash bin/up.sh` (needs a reachable Docker daemon) → open `http://localhost:$FRONTEND_PORT`:
  Portfolio shows the real account with live P&L; Refresh opens an MCP-bridge tab; Scan streams the
  screen; Pipeline/Debate run the live jury (needs `ANTHROPIC_API_KEY` in `backend/.env`).
- **Preconditions for the live paths:** Portfolio/Refresh need the host `robinhood-trading` MCP added
  and OAuth-authenticated (`claude mcp add --scope user --transport http robinhood-trading <URL>`);
  without it `/api/account` returns 503 with a message saying so. Debate/Pipeline need
  `ANTHROPIC_API_KEY`; without it they 503 and `/api/health` reports `debate_ready: false`.
  Scan and both test suites need neither.
- **Ports:** `bash bin/pick_ports.sh` twice → two distinct free ports each run, different across runs.
