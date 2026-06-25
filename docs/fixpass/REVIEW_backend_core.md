# Backend Core Review — Agentic Robinhood Dashboard

Reviewer: independent senior engineer (did not author this code).
Scope: `backend/app/{config,main,sse}.py`, routers `{health,account,refresh,scan,pipeline}.py`,
services `{snapshot,marks}.py`, `backend/Dockerfile`, `backend/requirements.txt`.
Out of scope (other reviewer): `backend/app/debate/*`.
Read-only context: `src/{data,screen,daily_scan,universe}.py`, `data/account_snapshot.json`.

Note: `ruff check` could not be run — Bash execution was denied in this environment.
Findings below are from static reading only; no linter pass was performed.

## Summary verdict

**REQUEST CHANGES**

The core read-only architecture is sound and the data-integrity / secret-handling story is genuinely
good (no order-placement path anywhere, API key never serialized, snapshot schema-validated, marks
soft-fail with NaN/non-positive rejection). However there are two real defects that block a clean
approval: a **paid endpoint (`/api/pipeline/run-stream`) that spends Anthropic tokens with no
rate-limit enforced** despite the config field existing for exactly that purpose, and an
**unbounded user-supplied ticker list on `/api/scan/run-stream`** that lets a single request fan out
an arbitrary number of blocking yfinance fetches. Both are cheap to fix. A non-atomic refresh-trigger
write and a TOCTOU/cooldown race round out the should-fix list.

## Bar checklist

| Item | Status | Notes |
|------|--------|-------|
| API key never reaches browser/logs/responses | PASS | health/account/refresh never serialize key; only boolean `debate_ready`. Lifespan log prints presence boolean, not value. config.py:30, health.py:24, main.py:32 |
| CORS scope sane | FAIL (soft) | Default `cors_origins="*"`; `allow_credentials=False` mitigates, but `*` default is too loose for a brokerage tool. config.py:59 |
| Ticker input validated `^[A-Z][A-Z.]{0,5}$` | PASS | scan.py:20, pipeline.py:23 both apply the exact regex. |
| Rate-limit on paid endpoints | FAIL | `debate_min_interval_seconds` exists (config.py:67) but pipeline.py never enforces it. |
| Snapshot schema-validated before use | PASS | snapshot.py:52–66, full pydantic validation, fails loud as 503. |
| Account strictly READ-ONLY (no order path) | PASS | No import of robinhood MCP / order placement anywhere in scope. Refresh only drops a trigger file. |
| No path traversal | PASS | All paths derive from config; no user-controlled path segments. |
| No injection | PASS | No shell/SQL; ticker regex-gated before reaching yfinance. |
| Async vs blocking (yfinance off event loop) | PASS | account.py:139 `to_thread`; scan.py:56 `to_thread`; pipeline.py:54 `to_thread`. marks fetched off-loop by callers. |
| Sync-def streaming endpoints don't block loop | PASS | scan/pipeline `run_stream` are sync but return immediately; the async generator body iterates on the loop with `to_thread` inside. |
| Type safety (pydantic v2) | PASS | Request/response models typed; `NonNegativeFloat` on prices. |
| No hardcoded secrets / debug prints / dead code | PASS | None found. `intraday_quantity` is unused but is valid schema (see NIT). |
| I/O failure handled gracefully | PARTIAL | snapshot/marks excellent; refresh write not atomic + unguarded race (see SHOULD-FIX). |
| Container write perms | PASS (with note) | appuser uid 1001 owns `/app`; data/logs are volume mounts — host mount must be writable by 1001 (deploy concern, Dockerfile:22). |

## Findings

### BLOCKER
- B1 — `pipeline.py` spends Anthropic tokens with no rate limit, despite `debate_min_interval_seconds` existing for exactly this. (pipeline.py:81–89)
- B2 — `scan.py` accepts an unbounded user-supplied `tickers` list; one request can fan out arbitrarily many blocking yfinance fetches (resource exhaustion / long-lived stream). (scan.py:23, 73–77)

### SHOULD-FIX
- S1 — Refresh trigger file written non-atomically; the host daemon can read a partial JSON line. (refresh.py:78–80)
- S2 — Refresh `exists()`→`write` TOCTOU and lock-free cooldown global; concurrent requests can both pass the gate and queue/over-spawn. (refresh.py:52–86)
- S3 — CORS default `"*"`; should default to explicit localhost origins for a brokerage dashboard. (config.py:59)
- S4 — Scan silently drops invalid tickers and can produce a no-op scan with no signal to the caller. (scan.py:74)

### NIT
- N1 — `TICKER_RE` duplicated verbatim in scan.py:20 and pipeline.py:23 (and the regex differs subtly from the `max_length` bounds). Hoist to one shared constant.
- N2 — `min_cap` has `gt=0` but no upper bound; a huge value yields an empty survivor set silently (acceptable but worth a sane ceiling). (scan.py:25)
- N3 — `intraday_quantity` is parsed into the snapshot schema but never used in P&L math; document why (read-only P&L uses settled+intraday total `quantity`) or drop it. (snapshot.py:23)
- N4 — `marks.get_marks` does not de-dupe concurrent in-flight fetches for the same symbol; two simultaneous cold requests both hit yfinance. Documented as soft; fine to leave.

### PRAISE
- P1 — No order-placement path anywhere in scope. The refresh "bridge" (drop a trigger file, host daemon does the MCP call) is a clean, correct way to keep the container credential-free and the dashboard truly read-only. (refresh.py module docstring, refresh.py:78–80)
- P2 — Secret hygiene is exemplary: the key is read once in config, the `_empty_key_is_none` validator (config.py:33–40) prevents the `""`-vs-`None` readiness mismatch, and every status surface (health, lifespan log) reports only a boolean. (config.py:30–40, health.py:24, main.py:32)
- P3 — `marks._fetch_one` is defensive exactly where it must be: fast_info→info fallback, NaN guard (`price == price`), non-positive rejection, never raises. The cache fetch happens *outside* the lock so a slow network call doesn't serialize all callers. (marks.py:22–63)
- P4 — snapshot.py validates a cross-trust-boundary file with pydantic and fails loud as a 503 instead of silently producing wrong P&L. The docstring states the trust boundary explicitly. (snapshot.py:1–7, 52–66)
- P5 — `_build_view` correctly soft-fails unpriced positions: they are flagged `priced=False`, excluded from `live_equity`, and surfaced via `stale_prices`, so a yfinance miss degrades the view rather than corrupting totals. (account.py:70–86, 121)

## Detailed findings

### B1 — Paid pipeline endpoint has no rate limit (pipeline.py:81–89)

```python
@router.post("/run-stream")
def run_stream(req: PipelineRequest):
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="...")
    ticker = req.ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail=f"Invalid ticker: {req.ticker!r}")
    return sse_response(_run_pipeline(ticker))
```

`_run_pipeline` calls `run_debate(ticker)`, which (per the charter) spends real Anthropic tokens —
a full jury (default `jury_size=10`) plus a synthesis model per call. The config defines
`debate_min_interval_seconds` (config.py:67) precisely to cap this, and the scope contract requires a
rate limit on paid endpoints. Nothing here enforces it. A held/refreshed button, or any client loop,
fires unbounded debates and bills the account's API key. (The debate router may enforce it
internally — that's the other reviewer's file — but pipeline.py exposes an independent paid path that
must enforce it too, or delegate to a shared guard.)

**Fix:** add a process-local min-interval gate mirroring refresh.py's cooldown (monotonic timestamp +
`debate_min_interval_seconds`), returning HTTP 429 with a `Retry-After`-style remaining seconds when
inside the window. Better: hoist the gate into a shared helper used by both the debate router and
pipeline so they share one budget rather than two independent ones. Guard the timestamp with a lock
(see S2) since FastAPI serves these from the event loop and the threadpool.

### B2 — Unbounded ticker list on scan stream (scan.py:23, 73–77)

```python
class ScanRequest(BaseModel):
    tickers: list[str] | None = Field(default=None)
    ...
if req.tickers:
    tickers = [t.strip().upper() for t in req.tickers if TICKER_RE.match(t.strip().upper())]
```

`tickers` has no length cap. A single POST with thousands of (valid-format) symbols launches that
many sequential `to_thread(_screen_one, ...)` yfinance fetches inside one long-lived SSE stream —
a cheap way to exhaust the threadpool / upstream rate budget and pin the worker for minutes. Even
without the API-key cost, scan should not let one request dictate unbounded blocking work.

**Fix:** add `max_length` to the field (e.g. `Field(default=None, max_length=200)`) so pydantic
rejects oversized lists with a 422, and/or truncate to a configured ceiling. Consider a per-client
or global concurrency cap on the scan stream as well.

### S1 — Non-atomic refresh trigger write (refresh.py:78–80)

```python
req_path.write_text(
    json.dumps({"requested_at": requested_at, "account": settings.agentic_account_masked}) + "\n"
)
```

The host daemon polls for `refresh.request` and parses it. `write_text` is not atomic — the daemon
can observe a zero-length or partially-written file and either fail to parse or act on garbage, and
the file's mere existence (checked at refresh.py:59 and refresh.py:98) flips `pending` true before the
content is durable.

**Fix:** write to a temp file in the same directory and `os.replace(tmp, req_path)` (atomic rename on
the same filesystem). This guarantees the daemon only ever sees a complete file. Wrap the write in
`try/except OSError` and surface a 503 rather than letting an `OSError` (e.g. read-only mount, see
Dockerfile:22 perms note) escape as an unhandled 500.

### S2 — Refresh TOCTOU + lock-free cooldown global (refresh.py:52–86)

`request_refresh` checks `req_path.exists()`, then checks the module global
`_last_request_monotonic`, then writes and updates the global — all without a lock. Two concurrent
requests can both pass the `exists()` check (file not yet written) and both pass the cooldown check
(neither has updated the global yet), defeating the "no tab storm" guarantee the docstring promises
(refresh.py:8–9). FastAPI dispatches sync-def endpoints to a threadpool, so concurrency here is real.

**Fix:** guard the read-modify-write of `_last_request_monotonic` and the existence/write sequence
with a `threading.Lock` (same pattern as marks.py:19). Combine with S1's atomic rename so the
existence check and the durable file flip together.

### S3 — CORS default is `"*"` (config.py:59)

```python
cors_origins: str = Field(default="*")
```

`allow_credentials=False` (main.py:50) prevents cookie/credential theft, so this is not a critical
hole, but a wildcard default lets any origin script the dashboard's endpoints (including the paid
pipeline). For a tool wired to a live brokerage snapshot and a billable API key, the *default* should
be the known-good localhost origins, with `*` opt-in via env only when explicitly needed.

**Fix:** default `cors_origins` to the concrete dev origins (e.g.
`"http://localhost:5173,http://127.0.0.1:5173"` plus whatever the frontend dev port is) and document
that operators widen it via env. The comment at config.py:57–58 already acknowledges the randomly
chosen port problem — solve it by listing the real origins rather than wildcarding.

### S4 — Invalid tickers silently dropped (scan.py:74)

When `req.tickers` is supplied, anything failing `TICKER_RE` is filtered out with no feedback. A
request of all-invalid symbols yields an empty `tickers` list and a `scan_complete` with zero
survivors — indistinguishable from "scanned everything, nothing passed." The caller can't tell their
input was rejected.

**Fix:** validate up front and reject with 400 listing the offending symbols (mirroring pipeline.py:88),
or emit a `scan_start`-level field reporting `{requested, accepted, rejected}` counts so the UI can
warn. Pydantic field-level validation with a custom validator is the cleanest place.

## Coordination observations

- **Shared rate-limit budget (debate ↔ pipeline).** B1's fix and the debate router (other reviewer's
  scope) should share one gate keyed on `debate_min_interval_seconds`, not two independent process
  globals — otherwise the combined spend is double the intended cap. Flag to whoever owns
  `debate/engine.py` and `routers/debate.py` so the guard lands in one shared module.
- **`TICKER_RE` lives in two routers** (scan.py:20, pipeline.py:23) and likely a third copy in the
  debate router. Consolidate into one module-level constant (e.g. `app/validation.py`) so the
  `^[A-Z][A-Z.]{0,5}$` contract can't drift between endpoints.
- **Dockerfile mount ownership (Dockerfile:22).** Container runs as uid 1001; `data/` and `logs/` are
  host volume mounts. Refresh writes (refresh.py) and lifespan `mkdir` (main.py:27) require the host
  mount to be writable by 1001. Confirm the compose/host setup grants this, or `mkdir`/`write_text`
  will raise at runtime. Coordinate with whoever owns docker-compose.yml.
- **`min_cap` ceiling and ticker-list cap** (B2, N2) are config-shaped; add the bounds to
  `config.py` so they're tunable alongside the other limits rather than hardcoded in the router.
