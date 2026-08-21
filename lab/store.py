"""Persistence for the Lab, against migration 023.

THE ONE JOB THIS MODULE HAS
    Translate a ValidationResult into the row 023 will accept, and refuse the ones it should not.
    The schema already makes a fabricated result unstorable — ck_model_runs_measured_agrees pins
    `metrics_measured` to the prediction counts in both directions — but a CHECK constraint fires
    with a message about a constraint, not about what went wrong. So the same invariant is stated
    here, in the language of the library, where the error can say which of two disagreeing sources
    was wrong.

    Those two sources are `ValidationResult.total_predictions` (successes only) plus
    `failed_predictions`, and the `measured` flag inside `metrics`. They are computed independently
    in validation.py and they must agree. When they do not, something in the validator changed
    underneath this code, and writing the row anyway would put the disagreement in the database
    where nobody would ever see it.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

logger = logging.getLogger("lab.store")

_POOL: ConnectionPool | None = None


class LabStoreUnavailable(RuntimeError):
    """No database. The Lab refuses to run rather than produce results it cannot record."""


class ResultInconsistent(RuntimeError):
    """The validator's two accounts of what it measured disagree. Nothing is written."""


def get_pool() -> ConnectionPool:
    """The Lab's own small pool, role rh_app.

    Deliberately not shared with the backend's pool: this is a different process in a different
    container, and it holds long-running training work that must not occupy a connection the API
    needs to render a page.
    """
    global _POOL
    if _POOL is None:
        dsn = os.environ.get("DATABASE_URL", "").strip()
        if not dsn:
            raise LabStoreUnavailable(
                "DATABASE_URL is unset. The Lab records every run before reporting it, so there is "
                "no degraded mode here — unlike the dashboard, a result nobody can look up later is "
                "not worth producing."
            )
        _POOL = ConnectionPool(dsn, min_size=1, max_size=4, open=True, timeout=10)
    return _POOL


@contextmanager
def connection():
    with get_pool().connection() as conn:
        conn.autocommit = True
        yield conn


# ── experiments ───────────────────────────────────────────────────────────────────────────────


def create_experiment(
    *,
    name: str,
    kind: str,
    data_source: str,
    dataset: str,
    validation_kind: str,
    params: dict | None = None,
    operator: str | None = None,
) -> int:
    """Open an experiment in `running` and return its id.

    Every argument that 023 declares mandatory is a required keyword here too. There is no default
    for data_source in either place, so no call site can forget to say what data it used.
    """
    with connection() as conn:
        return conn.execute(
            "INSERT INTO experiments"
            " (name, kind, data_source, dataset, validation_kind, params, status, operator)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'running', %s) RETURNING id",
            (
                name,
                kind,
                data_source,
                dataset,
                validation_kind,
                json.dumps(params or {}),
                operator,
            ),
        ).fetchone()[0]


def finish_experiment(experiment_id: int) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE experiments SET status = 'complete', completed_at = now(), updated_at = now()"
            " WHERE id = %s AND status = 'running'",
            (experiment_id,),
        )


def fail_experiment(experiment_id: int, error: str) -> None:
    """Close an experiment as failed, always with a reason.

    023 refuses a failed row with a blank error, so the fallback below is not politeness — without
    it, an exception whose str() is empty (a bare `raise ValueError()`) would make this UPDATE fail
    silently and leave the experiment stuck in `running` forever, looking like a live job.
    """
    message = (error or "").strip() or "failed with no message"
    with connection() as conn:
        conn.execute(
            "UPDATE experiments SET status = 'failed', completed_at = now(), updated_at = now(),"
            " error = %s WHERE id = %s AND status = 'running'",
            (message[:4000], experiment_id),
        )


# ── model runs ────────────────────────────────────────────────────────────────────────────────


def record_model_run(
    *,
    experiment_id: int,
    result: Any,
    params: dict | None = None,
    manifest: dict | None = None,
    artifact_path: str | None = None,
) -> int:
    """Write one validated model run. `result` is a ValidationResult.

    Raises ResultInconsistent — before touching the database — when the validator's prediction
    counts and its own `measured` flag disagree.
    """
    succeeded = int(result.total_predictions)
    failed = int(result.failed_predictions)
    attempted = succeeded + failed
    measured = succeeded > 0

    claimed = result.metrics.get("measured")
    if claimed is not None and bool(claimed) != measured:
        raise ResultInconsistent(
            f"{result.model_name}: metrics say measured={claimed!r} but the run produced "
            f"{succeeded} successful and {failed} failed prediction(s). These are computed "
            "independently in validation.py and must agree; something changed underneath this "
            "code. Nothing was written."
        )

    if failed:
        # Loud, not debug. A run whose metrics describe 12 of 400 predictions is technically
        # measured and practically meaningless, and the row alone will not shout about it.
        logger.warning(
            "%s: %d of %d predictions failed; metrics describe the surviving %d",
            result.model_name,
            failed,
            attempted,
            succeeded,
        )

    with connection() as conn:
        return conn.execute(
            "INSERT INTO model_runs"
            " (experiment_id, model_name, params, manifest, metrics, metrics_measured,"
            "  predictions_made, predictions_failed, artifact_path)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                experiment_id,
                result.model_name,
                json.dumps(params or {}),
                json.dumps(manifest or {}),
                json.dumps(result.metrics),
                measured,
                attempted,
                failed,
                artifact_path,
            ),
        ).fetchone()[0]


def set_baseline(model_name: str, run_id: int) -> None:
    """Move the baseline for one model. Clears the old one first — 023 allows only one.

    Both statements in one transaction: a crash between them would leave the model with no baseline
    at all, which is a worse state than the wrong one, because every comparison silently loses its
    reference point instead of being visibly wrong.
    """
    with get_pool().connection() as conn:  # transactional: autocommit deliberately NOT set
        conn.execute(
            "UPDATE model_runs SET is_baseline = false"
            " WHERE model_name = %s AND is_baseline",
            (model_name,),
        )
        conn.execute(
            "UPDATE model_runs SET is_baseline = true WHERE id = %s AND model_name = %s",
            (run_id, model_name),
        )


# ── sweeps ────────────────────────────────────────────────────────────────────────────────────


def record_sweep(
    *,
    experiment_id: int,
    param: str,
    metric: str,
    points: list[dict],
) -> int:
    """Write a sweep. The winner is chosen from measured points only, or there is no winner.

    `points` entries are {"value": ..., "metric": ...|None, "measured": bool}. A point that could
    not be measured keeps its place in the list — a sweep with holes must read as a sweep with
    holes — but cannot win.
    """
    measured_points = [p for p in points if p.get("measured") and p.get("metric") is not None]
    best = max(measured_points, key=lambda p: p["metric"], default=None)

    with connection() as conn:
        return conn.execute(
            "INSERT INTO sweeps"
            " (experiment_id, param, metric, points, best_value, best_metric_value,"
            "  points_tested, points_measured)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                experiment_id,
                param,
                metric,
                json.dumps(points),
                best["value"] if best else None,
                best["metric"] if best else None,
                len(points),
                len(measured_points),
            ),
        ).fetchone()[0]


# ── reading a result for what it actually is ──────────────────────────────────────────────────


def degenerate_reasons(metrics: dict) -> list[str]:
    """Ways a MEASURED result can still be worthless, derived from the metrics it already reports.

    `measured` answers "did anything get scored". It does not answer "is this a model". The first
    real run of this Lab — AAPL, 475 walk-forward predictions, zero failures — put elastic_net at
    the top of the Sharpe leaderboard at 1.58. It had called "up" on all 475 days: recall 1.0, zero
    false negatives, precision exactly equal to the base rate, and an information coefficient of
    -0.13. Its Sharpe was the market's, not the model's, and nothing in the row said so.

    None of these are errors, so none of them block anything. They are printed beside the number so
    a leaderboard cannot present a constant as a winner without saying what it is.
    """
    reasons: list[str] = []
    total = metrics.get("total_predictions") or 0
    if not metrics.get("measured") or total <= 0:
        return reasons

    recall = metrics.get("recall")
    tp = metrics.get("true_positives") or 0
    fp = metrics.get("false_positives") or 0
    fn = metrics.get("false_negatives") or 0

    # Called every day up: it caught every real up-day (recall 1.0) and missed none (fn 0), so it
    # never once predicted down. Its accuracy IS the base rate, which is why it needs saying.
    if recall == 1.0 and fn == 0 and (tp + fp) == total:
        reasons.append(
            "predicted UP on every sample — its accuracy is the class base rate, not a skill measure"
        )
    # Called every day down: it predicted the positive class zero times.
    elif (tp + fp) == 0:
        reasons.append(
            "predicted DOWN on every sample — its accuracy is 1 minus the base rate, not a skill "
            "measure"
        )

    # Profitable-looking while ranking backwards. A negative IC means the model's confidence is
    # anti-correlated with what happened; a positive Sharpe alongside it is the market's drift
    # showing through a directionally useless signal.
    ic = metrics.get("information_coefficient")
    sharpe = metrics.get("sharpe_ratio")
    if isinstance(ic, (int, float)) and isinstance(sharpe, (int, float)) and ic < 0 < sharpe:
        reasons.append(
            f"information coefficient is negative ({ic}) while Sharpe is positive ({sharpe}) — the "
            "signal ranks backwards and the return is the market's drift, not the model's"
        )
    return reasons


# ── reads ─────────────────────────────────────────────────────────────────────────────────────

_EXPERIMENT_COLUMNS = (
    "id, name, kind, data_source, dataset, params, validation_kind, status, operator,"
    " created_at, completed_at, error"
)


def _experiment_dict(row: tuple) -> dict:
    keys = [c.strip() for c in _EXPERIMENT_COLUMNS.split(",")]
    out = dict(zip(keys, row, strict=True))
    for key in ("created_at", "completed_at"):
        out[key] = out[key].isoformat() if out[key] else None
    return out


def list_experiments(limit: int = 50) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            f"SELECT {_EXPERIMENT_COLUMNS} FROM experiments ORDER BY created_at DESC LIMIT %s",
            (max(1, min(limit, 200)),),
        ).fetchall()
    return [_experiment_dict(r) for r in rows]


def get_experiment(experiment_id: int) -> dict | None:
    """One experiment with its model runs and sweeps."""
    with connection() as conn:
        row = conn.execute(
            f"SELECT {_EXPERIMENT_COLUMNS} FROM experiments WHERE id = %s", (experiment_id,)
        ).fetchone()
        if row is None:
            return None
        experiment = _experiment_dict(row)

        experiment["model_runs"] = [
            {
                "id": r[0],
                "model_name": r[1],
                "params": r[2],
                "manifest": r[3],
                "metrics": r[4],
                # Surfaced as its own field, never left buried in the metrics blob: this is the
                # flag that decides whether the numbers beside it mean anything.
                "metrics_measured": r[5],
                "predictions_made": r[6],
                "predictions_failed": r[7],
                "artifact_path": r[8],
                "is_baseline": r[9],
                "created_at": r[10].isoformat(),
            }
            for r in conn.execute(
                "SELECT id, model_name, params, manifest, metrics, metrics_measured,"
                " predictions_made, predictions_failed, artifact_path, is_baseline, created_at"
                " FROM model_runs WHERE experiment_id = %s ORDER BY id",
                (experiment_id,),
            ).fetchall()
        ]

        experiment["sweeps"] = [
            {
                "id": r[0],
                "param": r[1],
                "metric": r[2],
                "points": r[3],
                "best_value": float(r[4]) if r[4] is not None else None,
                "best_metric_value": float(r[5]) if r[5] is not None else None,
                "points_tested": r[6],
                "points_measured": r[7],
            }
            for r in conn.execute(
                "SELECT id, param, metric, points, best_value, best_metric_value,"
                " points_tested, points_measured FROM sweeps WHERE experiment_id = %s ORDER BY id",
                (experiment_id,),
            ).fetchall()
        ]
    return experiment


def leaderboard(metric: str = "sharpe_ratio", limit: int = 20) -> dict:
    """Best measured run per model, ranked.

    `metrics_measured` is in the WHERE clause AND is the predicate of ix_model_runs_rankable, so
    the filter is enforced twice — once by this query and once by the index it uses. That is
    deliberate belt-and-braces on the one thing that must never leak: a model that measured nothing
    appearing in a ranking as though it had.

    `metric` is validated against a fixed set rather than interpolated, because it names a JSON key
    inside a jsonb path expression and there is no bind parameter for an identifier.
    """
    if metric not in ALLOWED_METRICS:
        raise ValueError(f"unknown metric {metric!r}; expected one of {sorted(ALLOWED_METRICS)}")

    with connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (r.model_name)
                   r.model_name, r.id, r.metrics, r.predictions_made, r.predictions_failed,
                   r.is_baseline, e.data_source, e.dataset, r.created_at
              FROM model_runs r
              JOIN experiments e ON e.id = r.experiment_id
             WHERE r.metrics_measured
             ORDER BY r.model_name, (r.metrics ->> %s)::numeric DESC NULLS LAST, r.created_at DESC
            """,
            (metric,),
        ).fetchall()

    models = [
        {
            "model": r[0],
            "run_id": r[1],
            "metrics": r[2],
            "predictions_made": r[3],
            "predictions_failed": r[4],
            "is_baseline": r[5],
            # Carried onto every leaderboard row on purpose. A ranking that does not say which
            # rows came from generated data is a ranking that invites the comparison it should
            # prevent.
            "data_source": r[6],
            "dataset": r[7],
            "created_at": r[8].isoformat(),
            # Never blocks a row from ranking — it is printed beside the number, so a leaderboard
            # cannot present a constant predictor as a winner without saying what it is.
            "degenerate": degenerate_reasons(r[2]),
        }
        for r in rows
    ]
    models.sort(key=lambda m: m["metrics"].get(metric) or float("-inf"), reverse=True)

    with connection() as conn:
        unmeasured = conn.execute(
            "SELECT count(*) FROM model_runs WHERE NOT metrics_measured"
        ).fetchone()[0]

    return {
        "metric": metric,
        "models": models[:limit],
        # Reported, not hidden. A Lab with forty failed runs and two good ones should not look like
        # a Lab with two runs.
        "unmeasured_runs": unmeasured,
    }


ALLOWED_METRICS = frozenset(
    {
        "accuracy",
        "precision",
        "recall",
        "f1",
        "sharpe_ratio",
        "max_drawdown",
        "win_rate",
        "profit_factor",
        "information_coefficient",
    }
)


def health() -> dict:
    """Whether the Lab can reach its database AND find its schema. Booleans and counts only.

    Connectivity and schema are probed SEPARATELY, deliberately. Reporting `database: false` for a
    missing table would say the database is unreachable when it is fine and merely un-migrated —
    two problems with completely different fixes, and the first thing anyone does with an
    unreachable database is go looking at the network. Caught on the Lab's own first smoke test,
    which reported `database: false` against a perfectly healthy rh-db that had not yet had 023
    applied.

    Never returns a DSN: it carries the rh_app password.
    """
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
    except (LabStoreUnavailable, psycopg.Error) as exc:
        logger.error("lab database unreachable: %s", exc)
        return {"database": False, "schema": False, "error": type(exc).__name__}

    try:
        with connection() as conn:
            experiments = conn.execute("SELECT count(*) FROM experiments").fetchone()[0]
            bars = conn.execute("SELECT count(*) FROM price_bars_daily").fetchone()[0]
    except psycopg.Error as exc:
        logger.error("lab schema missing or unreadable: %s", exc)
        return {
            "database": True,
            "schema": False,
            "error": type(exc).__name__,
            "hint": "migration 023 has not been applied to this database",
        }

    return {"database": True, "schema": True, "experiments": experiments, "daily_bars": bars}
