"""GET /api/performance and GET /api/calibration.

Contracts: docs/contracts/performance-endpoint.md, docs/contracts/calibration-endpoint.md.
Read-only.

BOTH OF THESE READ TABLES THE LOOP FILLS, AND THE LOOP HAS BARELY RUN
    The equity curve starts the day the book was opened; the metrics need an evaluation run that has
    not happened; calibration needs judged debates that do not exist. All three are legitimately
    empty right now.

    So the shape that matters most here is the EMPTY one. A page that renders a blank chart is
    indistinguishable from a broken one, and "no data yet" is a different fact from "no data ever" or
    "the query failed". Each response says which, in words, with the reason — `status` and
    `reason` are not decoration, they are the difference between a page an operator trusts and one
    they learn to ignore.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.db import DbUnavailable, connection

logger = logging.getLogger("agentic.api.performance")

router = APIRouter(prefix="/api", tags=["performance"])

# The benchmark the curve is compared against. SPY rather than an index: it is a tradable series in
# the same bar table, so both legs of the comparison come from one source with one close convention.
BENCHMARK_SYMBOL = "SPY"


def _unavailable(exc: DbUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=f"The database is unavailable: {exc}")


@router.get("/performance")
def performance() -> dict[str, Any]:
    try:
        with connection() as conn:
            book = conn.execute(
                "SELECT id, inception_date, base_value FROM paper_portfolios"
                " WHERE kind = 'real' AND closed_at IS NULL ORDER BY id LIMIT 1"
            ).fetchone()
            if book is None:
                return _empty_performance(
                    "no_book",
                    "No real portfolio exists yet. It is created by db/sync_real_portfolio.py, "
                    "which mirrors the broker's holdings so they can be valued.",
                )
            portfolio_id, inception, base_value = book[0], book[1], float(book[2])

            marks = conn.execute(
                "SELECT trade_date, market_value, daily_return, cumulative_return, mark_kind"
                " FROM portfolio_returns_daily WHERE portfolio_id = %s ORDER BY trade_date",
                (portfolio_id,),
            ).fetchall()

            bench = conn.execute(
                "SELECT b.trade_date, b.close FROM price_bars_daily b"
                " JOIN securities s ON s.id = b.security_id"
                " WHERE upper(s.symbol) = %s AND b.trade_date >= %s ORDER BY b.trade_date",
                (BENCHMARK_SYMBOL, inception),
            ).fetchall()

            run = conn.execute(
                "SELECT window_start, window_end, walk_forward FROM evaluation_runs"
                " WHERE portfolio_id = %s ORDER BY window_end DESC LIMIT 1",
                (portfolio_id,),
            ).fetchone()
    except DbUnavailable as exc:
        raise _unavailable(exc) from None

    if not marks:
        return _empty_performance(
            "no_marks",
            "The book exists but has never been valued. bin/db_mark.sh live writes one mark per "
            "session, and it refuses any session it has no price bar for.",
            inception=str(inception),
        )

    # Rebase the benchmark to the book's inception so the two curves start together. Comparing an
    # absolute index level against a portfolio's cumulative return is a chart that looks like a
    # comparison and is not one.
    bench_curve: list[dict[str, Any]] = []
    if bench:
        first_close = float(bench[0][1])
        bench_curve = [
            {"date": str(d), "benchmark_cumulative_return": (float(c) / first_close) - 1.0}
            for d, c in bench
            if first_close
        ]

    curve = [
        {
            "date": str(d),
            "market_value": float(mv),
            "daily_return": float(dr) if dr is not None else None,
            "cumulative_return": float(cr) if cr is not None else None,
            "mark_kind": mk,
        }
        for d, mv, dr, cr, mk in marks
    ]

    return {
        "meta": {
            "status": "ok",
            "portfolio_id": portfolio_id,
            "inception_date": str(inception),
            "base_value": base_value,
            "sessions_marked": len(curve),
            "benchmark_symbol": BENCHMARK_SYMBOL,
            "returns_basis": "price_only",
            # Metrics need an evaluation run. Saying so beats rendering zeros that look computed.
            "metrics_status": "ok" if run else "no_evaluation_run",
            "metrics_reason": None if run else (
                "No evaluation run has been computed for this book yet, so Sharpe, Sortino and "
                "drawdown are unavailable — not zero."
            ),
        },
        "curve": curve,
        "benchmark": bench_curve,
        "metrics": None,
    }


def _empty_performance(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    """A 200 that says exactly why it is empty. Never a blank chart with no explanation."""
    return {
        "meta": {
            "status": status,
            "reason": reason,
            "benchmark_symbol": BENCHMARK_SYMBOL,
            "returns_basis": "price_only",
            "sessions_marked": 0,
            "metrics_status": status,
            "metrics_reason": reason,
            **extra,
        },
        "curve": [],
        "benchmark": [],
        "metrics": None,
    }


@router.get("/calibration")
def calibration(scope: str = Query("jury", pattern="^(jury|personas)$")) -> dict[str, Any]:
    """Stated confidence versus realised outcome.

    Empty until debates have been judged AND their outcomes scored. Both halves are needed: a
    confident call with no known outcome cannot be graded, and the contract is explicit that such
    rows are excluded from the bins rather than counted as misses.
    """
    try:
        with connection() as conn:
            judged = conn.execute("SELECT count(*) FROM judgments").fetchone()[0]
            debates = conn.execute("SELECT count(*) FROM debates").fetchone()[0]
    except DbUnavailable as exc:
        raise _unavailable(exc) from None

    if not judged:
        return {
            "meta": {
                "scope": scope,
                "status": "no_judgments",
                "reason": (
                    f"No judged debates on record ({debates} debate(s), {judged} judgment(s)). "
                    "Calibration compares stated confidence against realised outcomes, so it needs "
                    "both a judged call and a scored result — neither exists yet."
                ),
                "n": 0,
                "ece": None,
            },
            "bins": [],
            "items": [],
        }

    # Real scoring lands with the evaluation loop; until judgments exist there is nothing to bin,
    # and inventing a shape here would be a page that looks computed and is not.
    return {
        "meta": {"scope": scope, "status": "not_implemented", "n": judged, "ece": None,
                 "reason": "Judgments exist but scoring is not wired yet."},
        "bins": [],
        "items": [],
    }
