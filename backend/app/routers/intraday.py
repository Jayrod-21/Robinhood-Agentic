"""GET /api/intraday — the 30-minute ratio series (issue #133).

WHY THIS EXISTS SEPARATELY FROM THE COLLECTOR
    #135 built the tables, the arithmetic and the cron, and stopped at the database. So the series
    accumulated with no way to look at it: Jared asked whether any of it was visible in the UI, and
    the honest answer was that the frontend could not have rendered it even in principle, because
    nothing served it. A measurement nobody can read is most of the way to not having taken it.

WHAT IT WILL NOT DO
    Compute a ratio. Everything here is read from intraday_observations exactly as the collector
    stored it, alongside the formula_version that produced it. If a ratio is wrong, it is wrong in
    the table and the fix is a recompute — not a second implementation of the arithmetic living in
    a router, which is how the two come to disagree.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from app.db import DbUnavailable, connection

logger = logging.getLogger("agentic.api.intraday")

router = APIRouter(prefix="/api", tags=["intraday"])

# The shell polls this; a whole session for fifteen securities is ~200 rows, and a month is ~4,000.
MAX_POINTS = 2_000
DEFAULT_SESSIONS = 1

# Annotated form, so the Python defaults stay real values. With `sessions: int = Query(1, ...)` the
# default IS a Query object, and any caller that reaches the function without FastAPI's dependency
# resolution — a test, a job, another router — gets `Query >= 1` and a TypeError. FastAPI validates
# identically either way.
Symbol = Annotated[str | None, Query(max_length=16)]
Sessions = Annotated[int, Query(ge=1, le=30)]
Limit = Annotated[int, Query(ge=1, le=MAX_POINTS)]


@router.get("/intraday")
def intraday(
    symbol: Symbol = None,
    sessions: Sessions = DEFAULT_SESSIONS,
    limit: Limit = MAX_POINTS,
) -> dict[str, Any]:
    """The observation series, newest first.

    `symbol` narrows to one security; without it, every security in the most recent sessions.
    `sessions` counts TRADING sessions present in the table, not calendar days — asking for 5 over
    a holiday week should return five sessions of data, not three.
    """
    try:
        with connection() as conn:
            dates = conn.execute(
                "SELECT DISTINCT session_date FROM intraday_observations"
                " ORDER BY session_date DESC LIMIT %s",
                (sessions,),
            ).fetchall()
            if not dates:
                # An empty series is a real state — the collector may simply never have run — and
                # it is reported as one rather than as an error.
                return _empty("no observations recorded yet")

            oldest = dates[-1][0]
            rows = conn.execute(
                """
                SELECT s.symbol, o.observed_at, o.session_date, o.price, o.market_cap, o.volume,
                       o.pe_trailing, o.pe_forward, o.fcf_yield, o.scope_reasons,
                       o.fundamentals_id IS NOT NULL AS has_lineage, o.formula_version
                  FROM intraday_observations o
                  JOIN securities s ON s.id = o.security_id
                 WHERE o.session_date >= %s
                   AND (%s::text IS NULL OR upper(s.symbol) = upper(%s))
                 ORDER BY o.observed_at DESC, s.symbol
                 LIMIT %s
                """,
                (oldest, symbol, symbol, limit),
            ).fetchall()

            run = conn.execute(
                "SELECT started_at, status, scope_size, observed, failed, error"
                " FROM intraday_collection_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
    except DbUnavailable as exc:
        raise HTTPException(status_code=503, detail="the database is unavailable") from exc

    return {
        "meta": {
            "sessions": [str(d[0]) for d in dates],
            "symbol": symbol.upper() if symbol else None,
            "points": len(rows),
            "truncated": len(rows) >= limit,
            # The collector's own liveness. Without it an empty chart is ambiguous between "the
            # market is closed", "the collector died" and "this symbol left the watchlist" — the
            # same three-way ambiguity the runs table exists to resolve, carried through to the API
            # rather than left in the database.
            "last_run": _run(run),
        },
        "observations": [
            {
                "symbol": r[0],
                "observed_at": r[1].isoformat(),
                "session_date": str(r[2]),
                "price": float(r[3]),
                "market_cap": float(r[4]) if r[4] is not None else None,
                "volume": r[5],
                "pe_trailing": float(r[6]) if r[6] is not None else None,
                "pe_forward": float(r[7]) if r[7] is not None else None,
                "fcf_yield": float(r[8]) if r[8] is not None else None,
                "scope_reasons": list(r[9]),
                # Whether a statement row backed the ratios. A NULL ratio with lineage means the
                # filing did not carry that figure; without lineage it means there was no filing to
                # read. The page should be able to say which.
                "has_lineage": r[10],
                "formula_version": r[11],
            }
            for r in rows
        ],
    }


def _empty(reason: str) -> dict[str, Any]:
    return {
        "meta": {"sessions": [], "symbol": None, "points": 0, "truncated": False,
                 "last_run": None, "note": reason},
        "observations": [],
    }


def _run(row: tuple | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "started_at": row[0].isoformat(),
        "status": row[1],
        "scope_size": row[2],
        "observed": row[3],
        "failed": row[4],
        # Present for 'skipped' and 'failed' — usually "market closed", which is the single most
        # common reason this series has a gap and must not read as a fault.
        "error": row[5],
    }
