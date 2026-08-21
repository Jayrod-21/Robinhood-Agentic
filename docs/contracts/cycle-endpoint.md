# Contract: `GET /api/cycle/current` and `GET /api/cycle/runs`

Feeds a "what is the cycle doing" panel. Backend is **built and live**; the frontend is Joe's.

## Why polling, not a stream

`bin/scheduled_cycle.sh` runs the cycle through `docker compose exec`, which is a **different
process** from the uvicorn workers serving the pages. Nothing in memory crosses that boundary, so a
scheduled run's events can never reach an SSE stream this app is serving. Progress goes through
Postgres and the page reads it on an interval.

For a job that takes ~20 minutes end to end, a 10-second poll loses nothing a stream would give.
`meta.poll_seconds` carries the suggested interval so the page sizes itself off the source.

The existing `/api/debate/run-stream` still streams live for a debate **you** start from the UI.
That is unchanged and is a different thing from watching the scheduled cycle.

## `GET /api/cycle/current`

```jsonc
{
  "meta": {
    "poll_seconds": 10,
    "stale_after_minutes": 90,
    "has_ever_run": true,      // false = never recorded a cycle, NOT "none running now"
    "is_running": true
  },
  "run": {                      // null when nothing has ever run
    "id": 1,
    "phase": "close",           // "open" | "close"
    "status": "running",        // "running" | "complete" | "failed"
    "started_at": "2026-08-21T00:52:10+00:00",
    "updated_at": "2026-08-21T00:54:02+00:00",
    "completed_at": null,
    "total_positions": 15,      // null until the scan finishes and the book is read
    "completed_positions": 7,
    "current_symbol": "NVDA",   // null when not mid-debate
    "scanned": 25,
    "survivors": 1,
    "error": null,
    "progress_pct": 46.7        // null while total_positions is unknown — NOT 0
  },
  "recent": [ /* last 10, same shape as /api/cycle/runs */ ]
}
```

### States the UI has to tell apart

| Condition | Means |
|---|---|
| `run: null` | No cycle has **ever** been recorded. Different from a quiet Sunday, and only one is worth investigating. |
| `status: "running"`, `total_positions: null` | Scanning. The position count does not exist yet — show "scanning", not "0 of 0". |
| `progress_pct: null` | Total unknown. **Not 0%** — rendering it as 0 claims no progress on work that has not been sized. |
| `status: "failed"`, `error` set | It died. `error` says how, and is safe to display. |
| `status: "failed"`, error mentions "presumed" | **Inferred**, not reported: the process vanished without closing its row and the sweep marked it after 90 minutes. Worth showing differently from a reported failure — nobody observed this one fail. |

### Progress semantics

`completed_positions` counts debates that have **finished**, not started. Two run concurrently, so a
started-count would sit at 2 for eighty seconds and then jump. `current_symbol` is the most recently
completed name, which is the honest thing a per-debate granularity can report.

Per-debate, not per-juror, on purpose: a debate is ~80 seconds and eleven jurors, so per-juror
tracking would mean ~330 extra writes a day to render "6 of 10 jurors voted".

## `GET /api/cycle/runs?limit=20`

`{ "runs": [ { id, phase, status, started_at, completed_at, total_positions, completed_positions,
error, duration_seconds } ] }` — the history, newest first. `duration_seconds` is null while running.

## Errors

`503` when the database is unavailable, with a reason. Every other page keeps working; this is the
one that needs Postgres.
