# TESTS.md — 3b. Robinhood Agentic Trader

Consumed by `/testcheck`. Each suite lists its command and pass criteria.

**The authoritative gate is `bin/local_test.sh`**, which runs every suite in a container pinned to
CI's toolchain (`python:3.12-slim`, `node:20-slim`). Run it before any commit you care about:

```bash
bash bin/local_test.sh          # all six hard gates
bash bin/local_test.sh --fast   # skips database + frontend. NOT a gate — inner loop only.
```

Why it exists: this host runs Python **3.14** while CI and the deploy containers run **3.12**. A
green `.venv/bin/python -m pytest` is a claim about the host, not about what ships. Every suite
below is reproduced there under CI's versions, so a green run means CI will be green for the same
reasons rather than by coincidence. **If you bump an image or a tool version, bump it in
`.github/workflows/ci.yml` and `bin/local_test.sh` in the same commit** — the moment they drift,
this gate stops meaning anything.

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

### 1. Screen and adapter unit tests
- **Command:** `python3 -m pytest tests/ -q`
- **Pass criteria:** all tests pass. Covers the Sprinkle Sauce tier logic (liquidity floor,
  PEG/FCF/Piotroski gates, proportional single-snapshot scoring, ranking, `tests/test_screen.py`),
  the FMP field mapping that replaced yfinance's `.info` mapping (FCF-yield computation, PEG
  fallback, NaN/missing handling, `tests/test_fmp.py`), and — added for the 2026-08-17 broker
  switch — the Alpaca adapter (`tests/test_alpaca.py`: snapshot mapping, account masking,
  paper-vs-live `ALPACA_BASE_URL` recognition and normalization, credential-missing and
  secret-redaction error paths).
- **Network:** none required — all pure functions on synthetic fundamentals / mocked HTTP.

### 2. Dashboard backend unit tests
- **Command:** `python3 -m pytest backend/tests/ -q` (from the project root — running from
  `backend/` picks up `backend/.env` and flips auth enforcement on)
- **Pass criteria:** all tests pass. Covers jury aggregation (6+ majority decides, a true BUY/SELL
  directional deadlock escalates while a BUY/HOLD or SELL/HOLD tie resolves to HOLD, plurality holds,
  odd juries can't deadlock), account snapshot validation, broker source selection
  (`test_broker_source.py`: Alpaca preferred when configured, refusal — not snapshot fallback — on
  an unreachable Alpaca, snapshot fallback when Alpaca is unconfigured, the freshness cache), the
  live-marks overlay P&L/weights math + unpriced soft-fail, marks TTL caching, debate-record archive
  parsing + round-trip, the shared
  cooldown limiter (debate ↔ pipeline share one budget), the scan ticker-list cap + all-invalid
  rejection, the atomic refresh-trigger write + cooldown/pending gates, input validation, and — the
  security regression test — `get_record` rejecting path-traversal record ids.
- **Auth suites (AUTH_THREAT_MODEL §10), all inside this one command:**
  - `test_auth_routes.py` — §4 allow-list proven CLOSED over every reachable route (recursive
    enumeration; the FastAPI bootstrap routes /openapi.json, /docs, /redoc are disabled and pinned
    404), no password-reset-shaped route exists (§5.7), lookalike-prefix `/api/authz` is gated,
    CSRF composition, pre-deployment stand-down posture.
  - `test_auth_ratelimit.py` — the §5.1/§3.3 route-wide cooldown: per-route-not-per-account
    (spec-named test), one budget shared across every auth POST route, a blocked request never
    reaches Argon2, GET /api/auth/me exempt so page loads can't starve logins, per-app-instance
    gate, the default budget admits a full legitimate login flow, plus WindowLimiter unit
    behavior (window expiry, blocked calls don't extend the wait, non-positive disables).
  - `test_auth_cookie.py` (cookie attributes), `test_auth_db_failure.py` (fail-closed 503s),
    `test_auth_totp_window.py` (±1-step pin), `test_auth_db.py` (DB-backed semantics: lockout,
    replay high-water mark, recovery codes, sessions, verification, audit events —
    **testcontainers, needs Docker**), and `test_log_redaction.py` (secret scrubbing incl. the
    §5.4 `otpauth://`-URI and base32-secret rules).
- **Network:** none — yfinance/Anthropic calls are monkeypatched. Docker only, for
  `test_auth_db.py` and `test_outcomes_db.py` (throwaway testcontainers postgres).
  Plus the per-account slate resolution (`test_slate_per_account.py`): a slate governs ONE account
  and reconciliation never falls back to another account's targets, an account with no slate reports
  a state rather than a fifteen-row diff, and the numbered file wins for account 1 too. Plus the
  documentation-consistency suite (`test_docs_consistency.py`): every thesis is either in the slate
  or marked as excluded, dated prices in headings carry an as-of label, SLATE.md still parses and
  documents the resolution order it is subject to, the README does not claim an empty account, the
  charter lists its superseded facts, and the documented book size parses from its LABEL rather than
  from a prose sentence that any edit can break.

### 3. Frontend build
- **Command:** `cd frontend && npm install --no-audit --no-fund && npm run build`
- **Pass criteria:** `next build` compiles all routes with no type errors (/, /scan, /pipeline, /debate).

Suites 1, 2, and 3b can also be run together as a bare `python3 -m pytest` from the root —
`pyproject.toml` sets importlib import mode so the same-named `tests` packages don't collide.

### 3b. Migration runner + loader tests (needs Docker)
- **Command:** `python3 -m pytest db/tests/ -q`
- **Pass criteria:** all tests pass (currently 356). Ten layers: discovery tests for the
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
  ACTUAL migrations 001-007 through a full up → down → up cycle with schema-behavior assertions
  (restatement coexistence, partition spillover, symbol grammar, append-only provenance, and the
  evaluation-schema invariants: cascade/RESTRICT history protection, append-only grants,
  verified n_observations + expected_sessions/coverage_ratio, strategy-mode/kind tie);
  loader unit tests (`test_loader_units.py`: NYSE calendar rules vs reality, DST session bounds,
  DayBar fold incl. duplicate-bucket and string-money-path, corrupt-stream classification, FRED
  parse/retry, provider-error contract, argparse hardening); and the Phase A blocker regression
  suite (`test_loaders_db.py`: one test per fix-pass BLOCKER — B-1 loud provider failures,
  B-2 corrupt-archive survival, B-S1 as-of-bounded split factors, B-S2 required return_basis,
  B-S3/B-S4 catalog-comment truth + the 15:59-close behaviour pin, B-S5 splice/infer — each
  proven red-on-revert in docs/fixpass/FIX_REPORT_phaseA.md); and the auth store suites
  (AUTH_THREAT_MODEL §10): `test_auth_schema.py` — migration-applied auth schema invariants
  (rh_app/rh_auth grant separation, append-only auth_events, shape constraints, the
  single-live-verification-token partial index, clean down-migration) — and
  `test_manage_operator.py` — the host-CLI recovery path (seed with pinned argon2id params +
  encrypted TOTP secret + hashed single-use recovery codes, weak-password rejection, unlock,
  disable, reset-password/reset-totp with session revocation, exit-code contract, and no
  secret ever printed).
  Plus the Testing Lab store (`test_lab_store.py`): the mapping from a ValidationResult to a
  `model_runs` row, and the layer that catches an inconsistent result BEFORE the 023 CHECK
  constraint has to — `predictions_made` is attempts and not successes, the two directions of the
  measured/counts disagreement, a failure with no message still closing its experiment, a
  baseline move clearing the previous one transactionally, a sweep winner drawn from measured
  points only, a leaderboard that never surfaces an unmeasured run and labels a measured-but-
  degenerate one, and a health probe that tells an unreachable database apart from an unmigrated
  one. Plus 024's reconciliation columns on `cycle_runs` (`test_cycle_reconciliation.py`): a run
  that never reconciled leaves them NULL rather than zeroed, a partial write is refused, `in_sync`
  must agree with the counts in both directions, and desynced runs are indexed apart from runs that
  never checked. Plus instrument classification (`test_instrument_class.py`, `test_instrument_types_schema.py`):
  form is read from symbol convention BEFORE any provider list, because FMP's stock-list calls
  warrants "stock"; a four-letter symbol ending in W stays a company; `untracked` is not folded
  into `stock`; NULL is not investable; `security_type` is CHECK-constrained to the classifier's
  own vocabulary; and `non_common_instrument` is terminal to verify_daily_series check 7 while the
  six real companies stay non-terminal. Plus the intraday ratio log (`test_intraday_ratios.py`,
  `test_intraday_schema.py`): a negative P/E is recorded rather than nulled (loss-making and
  no-EPS are different facts), a NaN never reaches a stored value, a ratio cannot exist without the
  statement row it was computed from, `formula_version` has no default so a corrected formula stays
  applicable retroactively, `scope_reasons` uses `cardinality` not `array_length` (which returns
  NULL for an empty array, and a CHECK passes on NULL), and the runs table separates "never ran"
  from "ran and every quote failed" from "the market was closed". Plus the investable view
  (`test_investable_view.py`): the view admits exactly what `instrument_class.INVESTABLE` calls
  investable — share classes included, NULL excluded — so a universe filter cannot come to mean two
  different things in two containers.
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
- **Testing Lab, end to end (needs Docker + the live rh-db):**
  ```
  docker build -f lab/Dockerfile -t rh-lab:test .
  DSN=$(grep -o '^DATABASE_URL=.*' backend/.env | cut -d= -f2-)
  docker run --rm --name rh-lab-smoke -d --network rh-internal -e "DATABASE_URL=$DSN" rh-lab:test
  docker run --rm --network rh-internal curlimages/curl -sS \
    http://rh-lab-smoke:8100/api/testing-lab/health
  docker run --rm --network rh-internal curlimages/curl -sS -N -X POST \
    http://rh-lab-smoke:8100/api/testing-lab/experiments/run -H 'Content-Type: application/json' \
    -d '{"name":"smoke","models":["xgboost","random_forest","elastic_net"],
         "dataset":{"source":"historical_bars","symbol":"AAPL"}}'
  docker rm -f rh-lab-smoke
  ```
  Pass criteria: health reports `database: true, schema: true` and a non-zero `daily_bars`; the run
  streams a `dataset` event naming `historical_bars` and its class balance, one `model_result` per
  model with `measured: true`, and ends with `done`. Then
  `POST /experiments/run` with `"symbol":"NOSUCHTICKER"` must return an `error` event that says it
  is refusing rather than substituting synthetic data — never a result.
- **Full stack:** `bash bin/up.sh` (needs a reachable Docker daemon) → open `http://localhost:$FRONTEND_PORT`:
  Portfolio shows the real account with live P&L; Refresh opens an MCP-bridge tab; Scan streams the
  screen; Pipeline/Debate run the live jury (needs `ANTHROPIC_API_KEY` in `backend/.env`).
- **Preconditions for the live paths:** as of 2026-08-17, Portfolio (`/api/account`) prefers Alpaca —
  set `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` in `backend/.env` (`ALPACA_BASE_URL` selects
  paper vs. live; paper by default). If Alpaca is configured but unreachable, `/api/account` refuses
  rather than falling back. Without Alpaca credentials configured at all, Portfolio/Refresh fall back
  to the host `robinhood-trading` MCP added and OAuth-authenticated
  (`claude mcp add --scope user --transport http robinhood-trading <URL>`) and the
  `data/account_snapshot.json` file it refreshes; without either source, `/api/account` returns 503
  with a message saying so. Debate/Pipeline need `ANTHROPIC_API_KEY`; without it they 503 and
  `/api/health` reports `debate_ready: false`. Scan and both test suites need neither.
- **Ports:** `bash bin/pick_ports.sh` twice → two distinct free ports each run, different across runs.
