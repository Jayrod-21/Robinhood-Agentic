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


def _coverage(equity_curve: list[dict[str, Any]]) -> dict[str, Any]:
    """What share of the equity curve actually has a benchmark point beside it.

    Returns the `coverage` and `coverage_note` pair. The note names the missing span rather than
    just the count, because "0.87" tells an operator something is wrong and not where to look.
    """
    if not equity_curve:
        return {"coverage": None, "coverage_note": "No portfolio marks yet."}

    missing = [p["trade_date"] for p in equity_curve if p["benchmark_cumulative_return"] is None]
    covered = len(equity_curve) - len(missing)
    coverage = covered / len(equity_curve)

    if not missing:
        return {"coverage": 1.0, "coverage_note": None}
    if covered == 0:
        return {
            "coverage": 0.0,
            "coverage_note": (
                f"No {BENCHMARK_SYMBOL} bars since inception, so the benchmark line is absent."
            ),
        }
    span = missing[0] if len(missing) == 1 else f"{missing[0]} to {missing[-1]}"
    return {
        "coverage": round(coverage, 4),
        "coverage_note": (
            f"{len(missing)} of {len(equity_curve)} session(s) have no {BENCHMARK_SYMBOL} bar "
            f"({span}), so the benchmark line is broken there."
        ),
    }


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
            # MEASURED, not asserted. This was `1.0 if bench_by_date else None` — a binary "does
            # at least one SPY bar exist", reported as if it were the ratio the contract promises.
            # A benchmark with holes, or one whose bars lag the portfolio marks, produced a line
            # that quietly stopped mid-chart under a banner claiming 100% coverage, and the page's
            # `coverage < 1` warning could never fire because the value was never between 0 and 1.
            **_coverage(equity_curve),
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
        # None, not 0.0. `_f(rf) or 0.0` turned an unknown risk-free rate into a stated 0% — a
        # number the Sharpe was NOT computed against, presented as the one it was. It also collapsed
        # a genuine 0.0 and "we don't know" into the same value.
        "risk_free_annual": _f(rf),
    }


# ── calibration ───────────────────────────────────────────────────────────────────────────────

# Ten buckets across [0,1], the standard reliability-diagram grid.
_BINS = [(round(i / 10, 1), round((i + 1) / 10, 1)) for i in range(10)]
MIN_N_FOR_CALIBRATION = 30

# Must match db/score_judgments.py's default: the horizon the outcomes were actually scored at.
OUTCOME_HORIZON_DAYS = 5

# Built from the horizon, not restated beside it. Two copies of "5 trading days" is one edit away
# from an endpoint that declares a window it did not score against.
_OUTCOME_DEFINITION = f"counterfactual return positive over {OUTCOME_HORIZON_DAYS} trading days"


# Rows a call must be graded on. A judgment with no stated confidence cannot be calibrated — there
# is no claim to compare the outcome against — and one whose horizon has not elapsed has no outcome
# row at all. Both are EXCLUDED rather than counted as misses, which the contract is explicit about:
# scoring an unknown as a failure would make every recent call look like a bad one.
#
# JURY ONLY. This used to take the agent kind as a parameter, which made `scope=personas` look
# implemented: it filtered `agents.kind = 'persona'` against `judgments`, where `judge_kind` is a
# generated column pinned to 'judge' behind a composite foreign key. A persona judgment cannot
# exist, so that scope returned zero rows forever — while reporting the UNSCOPED judgment count
# beside a note telling the operator to wait for a five-session window. They were being told to
# wait for data that could never arrive. See `_persona_state` for what the scope does now.
_SCORED_SQL = """
SELECT a.agent_key, a.display_name, j.decision, j.confidence, o.is_correct,
       o.forward_return, o.outcome_date, s.symbol
  FROM judgments j
  JOIN judgment_outcomes o ON o.judgment_id = j.id
  JOIN agents a ON a.id = j.judge_agent_id
  JOIN debates d ON d.id = j.debate_id
  JOIN securities s ON s.id = d.security_id
 WHERE j.confidence IS NOT NULL
   AND a.kind = 'judge'
 ORDER BY o.outcome_date DESC, a.agent_key
"""

# The personas live in `agent_proposals` — the table the frontend contract names, and the one the
# old query never touched.
_PERSONA_STATE_SQL = """
SELECT count(*) AS proposals, count(confidence) AS with_confidence
  FROM agent_proposals
"""


def _bin_index(confidence: float) -> int:
    """Which reliability bucket a confidence falls in. 1.0 belongs to the top bucket, not a
    non-existent eleventh one."""
    return min(int(confidence * 10), len(_BINS) - 1)


def _summarise(rows: list[tuple]) -> dict[str, Any]:
    """Reliability statistics over (confidence, is_correct) pairs.

    ECE is the weighted mean gap between stated confidence and realised hit rate — how far the
    claims are from the truth. Brier is the mean squared error of the individual claims. They answer
    different questions: a forecaster can be perfectly calibrated in aggregate and still useless at
    ranking, which is why both are reported rather than one standing in for the other.
    """
    n = len(rows)
    if n == 0:
        return {
            "n_decisions": 0, "min_n_for_calibration": MIN_N_FOR_CALIBRATION,
            "is_calibratable": False, "ece": None, "brier": None,
            "base_rate": None, "mean_confidence": None,
            "bins": [{"lo": lo, "hi": hi, "predicted": None, "n": 0, "hit_rate": None}
                     for lo, hi in _BINS],
        }

    buckets: list[list[tuple[float, bool]]] = [[] for _ in _BINS]
    for confidence, correct in rows:
        buckets[_bin_index(confidence)].append((confidence, correct))

    bins, ece = [], 0.0
    for (lo, hi), members in zip(_BINS, buckets, strict=True):
        if not members:
            bins.append({"lo": lo, "hi": hi, "predicted": None, "n": 0, "hit_rate": None})
            continue
        mean_conf = sum(c for c, _ in members) / len(members)
        hit_rate = sum(1 for _, ok in members if ok) / len(members)
        ece += (len(members) / n) * abs(mean_conf - hit_rate)
        bins.append({
            "lo": lo, "hi": hi,
            "predicted": round(mean_conf, 4),
            "n": len(members),
            "hit_rate": round(hit_rate, 4),
        })

    brier = sum((c - (1.0 if ok else 0.0)) ** 2 for c, ok in rows) / n
    return {
        "n_decisions": n,
        "min_n_for_calibration": MIN_N_FOR_CALIBRATION,
        # Below the floor the numbers are computed and REPORTED, but flagged as not yet meaningful.
        # Hiding them would be worse: an operator cannot judge whether the sample is growing.
        "is_calibratable": n >= MIN_N_FOR_CALIBRATION,
        "ece": round(ece, 4),
        "brier": round(brier, 4),
        "base_rate": round(sum(1 for _, ok in rows if ok) / n, 4),
        "mean_confidence": round(sum(c for c, _ in rows) / n, 4),
        "bins": bins,
    }


def _persona_response(conn) -> dict[str, Any]:
    """The personas scope, told truthfully.

    Personas are the bull and bear researchers, and they live in `agent_proposals`. They are not
    calibratable today for a reason that has nothing to do with waiting: the engine asks them for a
    written case, not a probability, so every proposal on record has a NULL confidence. Calibration
    compares a stated confidence against an outcome; with no stated confidence there is no claim to
    grade, and no amount of elapsed time changes that.

    So this reports the real counts and names the actual blocker. The previous behaviour — an empty
    chart under "N judgments on record, none scored yet, wait for the five-session window" — pointed
    the operator at a clock instead of at the missing field.
    """
    proposals, with_confidence = conn.execute(_PERSONA_STATE_SQL).fetchone()
    if with_confidence:
        note = (
            f"{with_confidence} of {proposals} persona proposal(s) state a confidence, but their "
            f"outcome scoring is not implemented yet, so none are gradeable."
        )
    else:
        note = (
            f"Personas are not calibratable: {proposals} proposal(s) on record and none state a "
            f"confidence. The bull and bear researchers are asked for a written case, not a "
            f"probability, so there is no claim to compare an outcome against. This is not a "
            f"waiting state — it changes only if the engine starts asking them for one."
        )
    return {
        "meta": {
            "scope": "personas",
            "outcome_definition": _OUTCOME_DEFINITION,
            "outcome_horizon_days": OUTCOME_HORIZON_DAYS,
            "benchmark_relative": False,
            "returns_basis": "price_only",
            "priced_through": None,
            "coverage": 0.0 if proposals else None,
            "coverage_note": note,
        },
        "overall": _summarise([]),
        "by_agent": [],
        "decisions": [],
    }


@router.get("/calibration")
def calibration(scope: str = Query("jury", pattern="^(jury|personas)$")) -> dict[str, Any]:
    """Stated confidence versus realised outcome.

    Both halves are required: a confident call with no known outcome cannot be graded, and such rows
    are EXCLUDED from the bins rather than counted as misses.
    """
    try:
        with connection() as conn:
            if scope == "personas":
                return _persona_response(conn)
            rows = conn.execute(_SCORED_SQL).fetchall()
            judged = conn.execute(
                "SELECT count(*) FROM judgments j JOIN agents a ON a.id = j.judge_agent_id"
                " WHERE a.kind = 'judge'"
            ).fetchone()[0]
            debates = conn.execute("SELECT count(*) FROM debates").fetchone()[0]
            priced_through = conn.execute(
                "SELECT max(outcome_date)::text FROM judgment_outcomes"
            ).fetchone()[0]
    except DbUnavailable as exc:
        raise _unavailable(exc) from None

    overall = _summarise([(float(r[3]), bool(r[4])) for r in rows])

    by_agent = []
    for key in sorted({r[0] for r in rows}):
        mine = [r for r in rows if r[0] == key]
        stats = _summarise([(float(r[3]), bool(r[4])) for r in mine])
        by_agent.append({
            "agent_key": key,
            "display_name": mine[0][1] or key,
            "n_decisions": stats["n_decisions"],
            "ece": stats["ece"],
            "brier": stats["brier"],
            "hit_rate": stats["base_rate"],
            "mean_confidence": stats["mean_confidence"],
            "is_calibratable": stats["is_calibratable"],
        })

    coverage = (len(rows) / judged) if judged else None
    if not judged:
        note = (f"No judgments on record yet ({debates} debate(s)). Run a debate, then score it "
                f"with db/score_judgments.py.")
    elif not rows:
        note = (f"{judged} judgment(s) on record, none scored yet. A call is gradeable only once "
                f"its {OUTCOME_HORIZON_DAYS}-session window has elapsed and both price bars exist.")
    else:
        note = (f"{len(rows)} of {judged} judgment(s) scored. The rest are either inside their "
                f"{OUTCOME_HORIZON_DAYS}-session window, missing a price bar, or carry no stated "
                f"confidence to grade.")

    return {
        "meta": {
            "scope": scope,
            "outcome_definition": _OUTCOME_DEFINITION,
            "outcome_horizon_days": OUTCOME_HORIZON_DAYS,
            "benchmark_relative": False,
            "returns_basis": "price_only",
            "priced_through": priced_through,
            "coverage": round(coverage, 4) if coverage is not None else None,
            "coverage_note": note,
        },
        "overall": overall,
        "by_agent": by_agent,
        "decisions": [
            {
                "agent_key": r[0],
                "symbol": r[7],
                "decision": r[2],
                "confidence": float(r[3]),
                "is_correct": bool(r[4]),
                "forward_return": float(r[5]),
                "outcome_date": r[6].isoformat(),
            }
            for r in rows[:200]
        ],
    }
