"""GET /api/performance and GET /api/calibration.

Contracts: docs/contracts/performance-endpoint.md, docs/contracts/calibration-endpoint.md.
Read-only.

THE SHAPES HERE ARE JOE'S, NOT MINE
    frontend/src/lib/perf.ts and calibration.ts are the source of truth for field names and types,
    and both contracts say so. The first version of this module invented its own — `date` instead of
    `trade_date`, a separate benchmark array instead of `benchmark_cumulative_return` on each point,
    `n` instead of `n_observations`, `split` instead of `walk_forward`. Every field the page read
    came back undefined, and it crashed client-side after the request succeeded.

    That is worse than a 404. A missing endpoint says what is wrong; a 200 with the wrong shape
    looks like working software right up until the render.

BOTH READ TABLES THE LOOP FILLS, AND THE LOOP HAS BARELY RUN
    The equity curve starts the day the book was opened; metrics need an evaluation run; calibration
    needs judged debates with scored outcomes. All three are legitimately empty, so the EMPTY shape
    matters most — and it is still Joe's shape, with empty arrays and null metrics rather than an
    improvised error object the page has no branch for.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.db import DbUnavailable, connection

logger = logging.getLogger("agentic.api.performance")

router = APIRouter(prefix="/api", tags=["performance"])

# SPY rather than an index: a tradable series in the same bar table, so both legs of the comparison
# share one source and one close convention.
BENCHMARK_SYMBOL = "SPY"

# Below this many marks, ratios are arithmetic rather than evidence. Reported alongside every ratio
# so a number is never shown without the sample size that qualifies it.
MIN_N_FOR_RANKING = 60


def _unavailable(exc: DbUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=f"The database is unavailable: {exc}")


def _f(v):
    return float(v) if v is not None else None


@router.get("/performance")
def performance() -> dict[str, Any]:
    try:
        with connection() as conn:
            book = conn.execute(
                "SELECT id, kind, inception_date FROM paper_portfolios"
                " WHERE kind = 'real' AND closed_at IS NULL ORDER BY id LIMIT 1"
            ).fetchone()
            if book is None:
                return _empty(
                    "No real portfolio exists yet. db/sync_real_portfolio.py mirrors the broker's "
                    "holdings so they can be valued."
                )
            portfolio_id, kind, inception = book

            marks = conn.execute(
                "SELECT trade_date, market_value, cumulative_return"
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
                "SELECT window_start, window_end, n_observations, is_rankable, min_n_for_ranking,"
                " sharpe, sortino, max_drawdown, hit_rate, volatility, total_return,"
                " annualized_return, information_ratio, split, risk_free_annual"
                " FROM evaluation_runs WHERE portfolio_id = %s ORDER BY window_end DESC LIMIT 1",
                (portfolio_id,),
            ).fetchone()
    except DbUnavailable as exc:
        raise _unavailable(exc) from None

    if not marks:
        return _empty(
            "The book exists but has never been valued. bin/db_mark.sh live writes one mark per "
            "session, and refuses any session it has no price bar for.",
            portfolio_id=portfolio_id, kind=kind, inception_date=str(inception),
        )

    # Rebased to the book's inception so both curves start together. An absolute index level beside
    # a portfolio's cumulative return is a chart that looks like a comparison and is not one.
    bench_by_date: dict[str, float] = {}
    if bench:
        first = float(bench[0][1])
        if first:
            bench_by_date = {str(d): (float(c) / first) - 1.0 for d, c in bench}

    equity_curve = [
        {
            "trade_date": str(d),
            "market_value": float(mv),
            "cumulative_return": _f(cr),
            # On the point, not in a parallel array: the chart reads one series of records.
            "benchmark_cumulative_return": bench_by_date.get(str(d)),
        }
        for d, mv, cr in marks
    ]

    return {
        "meta": {
            "portfolio_id": portfolio_id,
            "kind": kind,
            "inception_date": str(inception),
            "benchmark_symbol": BENCHMARK_SYMBOL,
            "priced_through": equity_curve[-1]["trade_date"],
            "returns_basis": "price_only",
            "coverage": 1.0 if bench_by_date else None,
            "coverage_note": None if bench_by_date else (
                f"No {BENCHMARK_SYMBOL} bars since inception, so the benchmark line is absent."
            ),
        },
        "equity_curve": equity_curve,
        # Null rather than zeros. A Sharpe with no evaluation run is unavailable, not flat, and the
        # page has a branch for null that it does not have for a fabricated zero.
        "metrics": _metrics(run),
    }


def _empty(reason: str, **meta: Any) -> dict[str, Any]:
    """Joe's shape with nothing in it. Still his shape — an improvised error object would leave the
    page with no branch to take, which is how a 200 becomes a client-side crash."""
    return {
        "meta": {
            "portfolio_id": meta.get("portfolio_id"),
            "kind": meta.get("kind", "real"),
            "inception_date": meta.get("inception_date"),
            "benchmark_symbol": BENCHMARK_SYMBOL,
            "priced_through": None,
            "returns_basis": "price_only",
            "coverage": None,
            "coverage_note": reason,
        },
        "equity_curve": [],
        "metrics": None,
    }


def _metrics(run) -> dict[str, Any] | None:
    if run is None:
        return None
    (ws, we, n_obs, rankable, min_n, sharpe, sortino, max_dd, hit, vol, total, ann, info,
     split, rf) = run
    return {
        "window_start": str(ws),
        "window_end": str(we),
        "n_observations": n_obs,
        "is_rankable": bool(rankable),
        "min_n_for_ranking": min_n or MIN_N_FOR_RANKING,
        "sharpe": _f(sharpe),
        "sortino": _f(sortino),
        "max_drawdown": _f(max_dd),
        "hit_rate": _f(hit),
        "volatility": _f(vol),
        "total_return": _f(total),
        "annualized_return": _f(ann),
        "information_ratio": _f(info),
        "walk_forward": split,
        "risk_free_annual": _f(rf) or 0.0,
    }


# ── calibration ───────────────────────────────────────────────────────────────────────────────

# Ten buckets across [0,1], the standard reliability-diagram grid.
_BINS = [(round(i / 10, 1), round((i + 1) / 10, 1)) for i in range(10)]
MIN_N_FOR_CALIBRATION = 30


@router.get("/calibration")
def calibration(scope: str = Query("jury", pattern="^(jury|personas)$")) -> dict[str, Any]:
    """Stated confidence versus realised outcome.

    Empty until debates are judged AND their outcomes scored. Both halves are required: a confident
    call with no known outcome cannot be graded, and the contract is explicit that such rows are
    EXCLUDED from the bins rather than counted as misses — scoring an unknown as a failure would
    make every un-resolved call look like a bad one.
    """
    try:
        with connection() as conn:
            judged = conn.execute("SELECT count(*) FROM judgments").fetchone()[0]
            debates = conn.execute("SELECT count(*) FROM debates").fetchone()[0]
    except DbUnavailable as exc:
        raise _unavailable(exc) from None

    note = (
        f"No scored decisions yet ({debates} debate(s), {judged} judgment(s)). Calibration needs a "
        f"judged call AND a realised outcome; neither side exists yet."
    ) if not judged else (
        f"{judged} judgment(s) on record, but outcome scoring is not wired yet, so nothing is "
        f"gradeable."
    )

    return {
        "meta": {
            "scope": scope,
            # Stated, never implied: this choice drives the whole chart.
            "outcome_definition": "counterfactual return positive over 5 trading days",
            "outcome_horizon_days": 5,
            "benchmark_relative": False,
            "returns_basis": "price_only",
            "priced_through": None,
            "coverage": None,
            "coverage_note": note,
        },
        "overall": {
            "n_decisions": 0,
            "min_n_for_calibration": MIN_N_FOR_CALIBRATION,
            "is_calibratable": False,
            "ece": None,
            "brier": None,
            "base_rate": None,
            "mean_confidence": None,
            # Every bucket present with n=0, so the diagram draws its grid rather than collapsing.
            "bins": [{"lo": lo, "hi": hi, "predicted": None, "n": 0, "hit_rate": None}
                     for lo, hi in _BINS],
        },
        "by_agent": [],
        "decisions": [],
    }
