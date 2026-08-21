"""Progress for the twice-daily cycle: what is running, how far in, and what happened last time.

WHY THIS IS WRITTEN TO POSTGRES
    The cycle runs as `docker compose exec backend python -m app.jobs.cycle` — a DIFFERENT PROCESS
    from the uvicorn workers serving the dashboard. Nothing in memory is visible across that
    boundary, so a publisher/subscriber inside the app could never show a scheduled run. The
    database is the only channel, and it survives a restart into the bargain.

EVERY WRITE IS BEST-EFFORT
    Progress reporting must never be able to fail the work it reports on. A cycle that dies because
    it could not record that it was 7 of 15 through has traded the thing for the story about the
    thing. Every function here swallows and logs.

STALE RUNS ARE SWEPT, NOT TRUSTED
    A crashed or killed cycle leaves a row saying 'running' forever, and a stale 'running' row is
    worse than no row: it reports a cycle in progress that no process is executing. So a run older
    than the sweep window is reported as failed with that stated as the reason — inferred, and
    labelled as inferred.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import DbUnavailable, connection

logger = logging.getLogger("agentic.services.cycle_state")

# Past this with no update, a 'running' row is presumed dead. A full cycle is ~20 minutes at fifteen
# positions; 90 gives generous headroom for a slow provider without leaving a corpse on the page
# for hours.
STALE_AFTER_MINUTES = 90


def start(phase: str, total_positions: int | None = None) -> int | None:
    """Open a run. Returns its id, or None when the database is unreachable."""
    try:
        with connection() as conn:
            row = conn.execute(
                "INSERT INTO cycle_runs (phase, total_positions) VALUES (%s, %s) RETURNING id",
                (phase, total_positions),
            ).fetchone()
        return row[0]
    except Exception as exc:  # noqa: BLE001 — telemetry must never fail the cycle
        logger.warning("could not open a cycle_runs row: %s", exc)
        return None


def update(run_id: int | None, **fields: Any) -> None:
    """Patch the run. Unknown columns are ignored rather than raising mid-cycle."""
    if run_id is None or not fields:
        return
    allowed = {
        "total_positions", "completed_positions", "current_symbol", "scanned", "survivors",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    assignments = ", ".join(f"{k} = %s" for k in sets)
    try:
        with connection() as conn:
            conn.execute(
                f"UPDATE cycle_runs SET {assignments}, updated_at = now() WHERE id = %s",
                (*sets.values(), run_id),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not update cycle_runs %s: %s", run_id, exc)


def finish(run_id: int | None, *, error: str | None = None) -> None:
    """Close the run. `error` present means it failed, and says why."""
    if run_id is None:
        return
    try:
        with connection() as conn:
            conn.execute(
                "UPDATE cycle_runs SET status = %s, error = %s, completed_at = now(),"
                " updated_at = now(), current_symbol = NULL WHERE id = %s",
                ("failed" if error else "complete", error, run_id),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not close cycle_runs %s: %s", run_id, exc)


def sweep_stale(conn) -> int:
    """Mark abandoned 'running' rows as failed. Returns how many were swept.

    A killed process cannot close its own row, so without this a crashed cycle reports as in
    progress indefinitely — which is exactly the "is it running?" question this table was added to
    answer, answered wrongly and confidently.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_AFTER_MINUTES)
    result = conn.execute(
        "UPDATE cycle_runs SET status = 'failed', completed_at = now(), current_symbol = NULL,"
        " error = %s WHERE status = 'running' AND updated_at < %s",
        (
            (
                f"No progress for over {STALE_AFTER_MINUTES} minutes; the run is presumed to have "
                f"died. This status was inferred, not reported by the job."
            ),
            cutoff,
        ),
    )
    return result.rowcount or 0


def current() -> dict[str, Any] | None:
    """The active run, or the most recent finished one. None when nothing has ever run."""
    try:
        with connection() as conn:
            swept = sweep_stale(conn)
            if swept:
                logger.warning("swept %d abandoned cycle run(s)", swept)
            row = conn.execute(
                "SELECT id, phase, status, started_at, updated_at, completed_at, total_positions,"
                " completed_positions, current_symbol, scanned, survivors, error"
                " FROM cycle_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
    except DbUnavailable:
        raise
    if row is None:
        return None

    (rid, phase, status, started, updated, completed, total, done, symbol,
     scanned, survivors, error) = row
    return {
        "id": rid,
        "phase": phase,
        "status": status,
        "started_at": started.isoformat(),
        "updated_at": updated.isoformat(),
        "completed_at": completed.isoformat() if completed else None,
        "total_positions": total,
        "completed_positions": done,
        "current_symbol": symbol,
        "scanned": scanned,
        "survivors": survivors,
        "error": error,
        # Computed here rather than on the page so two clients cannot disagree about it. None when
        # the total is unknown, which is different from 0% — the scan runs before the count exists.
        "progress_pct": (round(100.0 * done / total, 1) if total else None),
    }


def recent(limit: int = 10) -> list[dict[str, Any]]:
    with connection() as conn:
        sweep_stale(conn)
        rows = conn.execute(
            "SELECT id, phase, status, started_at, completed_at, total_positions,"
            " completed_positions, error FROM cycle_runs ORDER BY started_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [
        {
            "id": r[0], "phase": r[1], "status": r[2],
            "started_at": r[3].isoformat(),
            "completed_at": r[4].isoformat() if r[4] else None,
            "total_positions": r[5], "completed_positions": r[6], "error": r[7],
            "duration_seconds": (r[4] - r[3]).total_seconds() if r[4] else None,
        }
        for r in rows
    ]
