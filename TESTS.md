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

Suites 1, 2, and 3b can also be run together as a bare `python3 -m pytest` from the root —
`pyproject.toml` sets importlib import mode so the same-named `tests` packages don't collide.

### 3b. Migration runner unit + integration tests (needs Docker)
- **Command:** `python3 -m pytest db/tests/ -q`
- **Pass criteria:** all tests pass (currently 104). Three layers: discovery tests for the
  filename-based destructive classification (ADR-002: `NNN_name.destructive.{up,down}.sql`),
  loud rejection of near-miss filenames (uppercase `.SQL`, trailing junk — never silently
  skipped), byte-level rejection (NUL / BOM / invalid UTF-8), the best-effort keyword sniff
  (comment-separated keywords included) plus pins of its documented holes (dynamic SQL and
  `DELETE FROM` are NOT caught — the filename marker is the control there), `--target`
  and exit-code contracts, plus a multi-MB linearity check; testcontainers-backed integration
  (atomicity, checksum halt, destructive gate end-to-end, the pinned four-round forgery corpus
  proving the CLASSIFICATION cannot be forged from contents and the sniff still refuses the
  literal shapes, and the server-enforced transaction-ownership
  check — stray `COMMIT`/`ROLLBACK`/`COMMIT;BEGIN` detected via libpq status + xid); and the
  ACTUAL migrations 001-003 through a full up → down → up cycle with schema-behavior assertions
  (restatement coexistence, partition spillover, symbol grammar, append-only provenance).
- **Network:** Docker only — testcontainers spins a throwaway postgres:16-alpine; the live rh-db
  is never touched.

### 4. Database migrations (needs Docker)
- **Command:** `bash bin/db_up.sh && bash bin/db_migrate.sh up`
- **Pass criteria:** all migrations apply; `bash bin/db_migrate.sh status` shows every version
  `applied` with checksum `ok`.
- **Round trip:** `bash bin/db_migrate.sh down --allow-destructive --target 000` then `up` again —
  proves each `.down.sql` truly reverses its `.up.sql` and leaves no residue.
- **Guard checks** (each must fail loudly): `down` without `--allow-destructive` → exit 1; editing an
  applied migration → `ChecksumMismatch`, exit 1; a `DROP TABLE`/`TRUNCATE` migration whose filename
  lacks the `.destructive` marker → refused at discovery, exit 1 (best-effort: dynamic SQL and
  `DELETE FROM` are not sniffable — mark the filename); a near-miss filename (`.SQL`, trailing
  junk, version-prefixed stray) → refused at discovery, exit 1, never silently skipped; a
  migration that issues its own
  `COMMIT`/`ROLLBACK` → detected post-execution (libpq status + xid), never recorded, exit 1; a NUL
  byte / BOM / invalid UTF-8 in a file → refused at discovery, exit 1; an unpadded/unknown
  `--target` → exit 1; a CLI typo → exit 1 (never 2, which is reserved for SQL failures); bad
  credentials → exit 3.
- **Backups:** `bash bin/db_backup.sh` → dumps to `data/backups/db/`, verifies via
  `pg_restore --list`, prunes to the newest 14.
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
