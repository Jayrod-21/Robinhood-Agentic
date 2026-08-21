"""lab/store.py against the real 023 schema: the mapping from a ValidationResult to a row.

The schema (023) already makes a fabricated result unstorable. What this suite covers is the layer
above it — whether the Lab's own code fills those columns from the right fields, and whether it
catches the disagreement BEFORE the database has to.

That distinction matters. A CHECK constraint firing tells you a constraint was violated; it does not
tell you that `total_predictions` counts successes while `failed_predictions` counts failures and
the two were added wrong. `record_model_run` raises ResultInconsistent with that sentence in it,
which is the difference between a five-minute fix and an afternoon.

Never touches the live rh-db — the container is ephemeral and dies with the session.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest

try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
    from testcontainers.community.postgres import PostgresContainer
except ImportError:  # pragma: no cover
    from testcontainers.postgres import PostgresContainer

from migrate import EXIT_OK
from migrate import main as migrate_main

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_MIGRATIONS = REPO_ROOT / "db" / "migrations"
PG_IMAGE = "postgres:16-alpine"


@dataclass
class _Result:
    """The shape of validation.ValidationResult, without importing numpy into this suite."""

    model_name: str = "xgboost"
    total_steps: int = 5
    total_predictions: int = 180
    failed_predictions: int = 0
    metrics: dict = field(
        default_factory=lambda: {"measured": True, "sharpe_ratio": 1.34, "accuracy": 0.57}
    )


@pytest.fixture(scope="session")
def store_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


@pytest.fixture
def store(store_pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch):
    """lab.store bound to a fresh migrated database, with its module-level pool reset per test."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from lab import store as lab_store

    name = f"labstore_{uuid.uuid4().hex[:12]}"
    admin = (
        f"postgresql://{store_pg.username}:{store_pg.password}"
        f"@{store_pg.get_container_host_ip()}:{store_pg.get_exposed_port(5432)}/{store_pg.dbname}"
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    monkeypatch.setenv("DATABASE_URL", url)
    assert migrate_main(["up", "--migrations-dir", str(REPO_MIGRATIONS)]) == EXIT_OK

    # The pool is module-level and cached; without this a second test reuses the first's database.
    monkeypatch.setattr(lab_store, "_POOL", None)
    yield lab_store
    if lab_store._POOL is not None:
        lab_store._POOL.close()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def _experiment(store, **kw) -> int:
    args = {
        "name": "nightly",
        "kind": "walk_forward",
        "data_source": "synthetic",
        "dataset": "seed=42 bars=750",
        "validation_kind": "walk_forward",
    }
    args.update(kw)
    return store.create_experiment(**args)


# ── the consistency check, which fires before the database sees anything ──────────────────────


def test_a_result_whose_two_accounts_disagree_is_refused_before_it_is_written(store) -> None:
    """metrics say measured, the counts say nothing survived. validation.py computes these
    independently; when they disagree, something changed underneath this code."""
    experiment_id = _experiment(store)
    result = _Result(
        total_predictions=0,
        failed_predictions=200,
        metrics={"measured": True, "sharpe_ratio": 2.0},
    )

    with pytest.raises(store.ResultInconsistent, match="Nothing was written"):
        store.record_model_run(experiment_id=experiment_id, result=result)

    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM model_runs").fetchone()[0] == 0


def test_the_other_direction_of_the_disagreement_is_refused_too(store) -> None:
    experiment_id = _experiment(store)
    result = _Result(
        total_predictions=180, failed_predictions=0, metrics={"measured": False}
    )

    with pytest.raises(store.ResultInconsistent):
        store.record_model_run(experiment_id=experiment_id, result=result)


def test_predictions_made_is_attempts_not_successes(store) -> None:
    """Break: write total_predictions into predictions_made. The row then claims 180 attempts when
    there were 200, and the failure rate silently drops from 10% to 0."""
    experiment_id = _experiment(store)
    run_id = store.record_model_run(
        experiment_id=experiment_id,
        result=_Result(total_predictions=180, failed_predictions=20),
    )

    with store.connection() as conn:
        made, failed, measured = conn.execute(
            "SELECT predictions_made, predictions_failed, metrics_measured"
            " FROM model_runs WHERE id = %s",
            (run_id,),
        ).fetchone()

    assert (made, failed) == (200, 20)
    assert measured is True


def test_a_run_that_measured_nothing_is_stored_as_unmeasured(store) -> None:
    experiment_id = _experiment(store)
    run_id = store.record_model_run(
        experiment_id=experiment_id,
        result=_Result(
            total_predictions=0, failed_predictions=90, metrics={"measured": False, "accuracy": 0.0}
        ),
    )

    with store.connection() as conn:
        assert conn.execute(
            "SELECT metrics_measured FROM model_runs WHERE id = %s", (run_id,)
        ).fetchone()[0] is False


# ── experiment lifecycle ──────────────────────────────────────────────────────────────────────


def test_an_experiment_opens_running_and_closes_complete(store) -> None:
    experiment_id = _experiment(store)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT status, completed_at FROM experiments WHERE id = %s", (experiment_id,)
        ).fetchone() == ("running", None)

    store.finish_experiment(experiment_id)
    with store.connection() as conn:
        status, completed = conn.execute(
            "SELECT status, completed_at FROM experiments WHERE id = %s", (experiment_id,)
        ).fetchone()
    assert status == "complete"
    assert completed is not None


def test_a_failure_with_no_message_still_closes_the_experiment(store) -> None:
    """023 refuses a failed row with a blank error. Without the fallback in fail_experiment, a bare
    `raise ValueError()` would leave the row stuck in `running` forever, looking like a live job."""
    experiment_id = _experiment(store)
    store.fail_experiment(experiment_id, "")

    with store.connection() as conn:
        status, error = conn.execute(
            "SELECT status, error FROM experiments WHERE id = %s", (experiment_id,)
        ).fetchone()
    assert status == "failed"
    assert error and error.strip()


def test_finishing_an_already_failed_experiment_does_not_resurrect_it(store) -> None:
    """Both UPDATEs are guarded on status = 'running'. A late success event after a failure must not
    overwrite the recorded reason."""
    experiment_id = _experiment(store)
    store.fail_experiment(experiment_id, "out of memory")
    store.finish_experiment(experiment_id)

    with store.connection() as conn:
        status, error = conn.execute(
            "SELECT status, error FROM experiments WHERE id = %s", (experiment_id,)
        ).fetchone()
    assert status == "failed"
    assert error == "out of memory"


# ── baselines and sweeps ──────────────────────────────────────────────────────────────────────


def test_moving_a_baseline_clears_the_previous_one(store) -> None:
    """023 permits only one baseline per model, so a set without a clear would raise. Both happen in
    one transaction: a crash between them leaves the model with NO baseline, which is worse than the
    wrong one — every comparison silently loses its reference point instead of being visibly wrong."""
    experiment_id = _experiment(store)
    first = store.record_model_run(experiment_id=experiment_id, result=_Result())
    second = store.record_model_run(experiment_id=experiment_id, result=_Result())

    store.set_baseline("xgboost", first)
    store.set_baseline("xgboost", second)

    with store.connection() as conn:
        baselines = conn.execute(
            "SELECT id FROM model_runs WHERE model_name = 'xgboost' AND is_baseline"
        ).fetchall()
    assert [r[0] for r in baselines] == [second]


def test_a_sweep_winner_comes_from_measured_points_only(store) -> None:
    """The unmeasured point has the highest metric value in the list and must still not win."""
    experiment_id = _experiment(store, kind="sweep")
    sweep_id = store.record_sweep(
        experiment_id=experiment_id,
        param="max_depth",
        metric="sharpe_ratio",
        points=[
            {"value": 2, "metric": 0.4, "measured": True},
            {"value": 4, "metric": 9.9, "measured": False},
            {"value": 6, "metric": 1.1, "measured": True},
        ],
    )

    with store.connection() as conn:
        best_value, best_metric, tested, measured = conn.execute(
            "SELECT best_value, best_metric_value, points_tested, points_measured"
            " FROM sweeps WHERE id = %s",
            (sweep_id,),
        ).fetchone()

    assert float(best_value) == 6.0
    assert float(best_metric) == 1.1
    assert (tested, measured) == (3, 2)


def test_a_sweep_with_no_measured_points_records_no_winner(store) -> None:
    experiment_id = _experiment(store, kind="sweep")
    sweep_id = store.record_sweep(
        experiment_id=experiment_id,
        param="max_depth",
        metric="sharpe_ratio",
        points=[{"value": 2, "metric": None, "measured": False}],
    )

    with store.connection() as conn:
        assert conn.execute(
            "SELECT best_value, best_metric_value FROM sweeps WHERE id = %s", (sweep_id,)
        ).fetchone() == (None, None)


# ── the leaderboard ───────────────────────────────────────────────────────────────────────────


def test_the_leaderboard_never_shows_a_run_that_measured_nothing(store) -> None:
    experiment_id = _experiment(store)
    store.record_model_run(
        experiment_id=experiment_id,
        result=_Result(model_name="xgboost", metrics={"measured": True, "sharpe_ratio": 1.0}),
    )
    store.record_model_run(
        experiment_id=experiment_id,
        result=_Result(
            model_name="arima",
            total_predictions=0,
            failed_predictions=40,
            metrics={"measured": False, "sharpe_ratio": 0.0},
        ),
    )

    board = store.leaderboard("sharpe_ratio")

    assert [m["model"] for m in board["models"]] == ["xgboost"]
    # Counted, not hidden. A Lab with forty failed runs and two good ones must not look like a Lab
    # with two runs.
    assert board["unmeasured_runs"] == 1


def test_every_leaderboard_row_says_what_data_it_was_measured_on(store) -> None:
    """A ranking that does not distinguish generated data from real history invites exactly the
    comparison it should prevent."""
    synthetic_run = _experiment(store, data_source="synthetic", dataset="seed=1")
    real_run = _experiment(store, data_source="historical_bars", dataset="AAPL 2021..2026")
    store.record_model_run(
        experiment_id=synthetic_run,
        result=_Result(model_name="xgboost", metrics={"measured": True, "sharpe_ratio": 3.0}),
    )
    store.record_model_run(
        experiment_id=real_run,
        result=_Result(model_name="elastic_net", metrics={"measured": True, "sharpe_ratio": 0.8}),
    )

    board = store.leaderboard("sharpe_ratio")
    by_model = {m["model"]: m for m in board["models"]}

    assert by_model["xgboost"]["data_source"] == "synthetic"
    assert by_model["elastic_net"]["data_source"] == "historical_bars"
    assert by_model["elastic_net"]["dataset"] == "AAPL 2021..2026"


def test_the_leaderboard_is_ranked_by_the_requested_metric(store) -> None:
    experiment_id = _experiment(store)
    for model, sharpe in (("xgboost", 0.5), ("random_forest", 2.1), ("elastic_net", 1.3)):
        store.record_model_run(
            experiment_id=experiment_id,
            result=_Result(model_name=model, metrics={"measured": True, "sharpe_ratio": sharpe}),
        )

    board = store.leaderboard("sharpe_ratio")
    assert [m["model"] for m in board["models"]] == ["random_forest", "elastic_net", "xgboost"]


def test_an_unknown_metric_is_rejected_rather_than_interpolated(store) -> None:
    """`metric` names a JSON key inside a jsonb path expression, and there is no bind parameter for
    an identifier — so it is checked against a fixed set instead of reaching the query."""
    with pytest.raises(ValueError, match="unknown metric"):
        store.leaderboard("'; DROP TABLE model_runs; --")


def test_health_reports_booleans_and_counts_and_never_the_dsn(store) -> None:
    health = store.health()

    assert health["database"] is True
    assert health["schema"] is True
    assert health["experiments"] == 0
    assert "daily_bars" in health
    assert not any("postgresql://" in str(v) for v in health.values())


def test_health_separates_an_unreachable_database_from_an_unmigrated_one(store) -> None:
    """The Lab's first smoke test reported `database: false` against a perfectly healthy rh-db that
    simply had not had 023 applied yet — two problems with different fixes, reported identically.
    Break: collapse the two probes back into one try block."""
    with store.connection() as conn:
        conn.execute("DROP TABLE sweeps, model_runs, experiments")

    health = store.health()

    assert health["database"] is True, "the connection is fine; only the schema is missing"
    assert health["schema"] is False
    assert "023" in health["hint"]


# ── measured is not the same as meaningful ────────────────────────────────────────────────────


def test_an_always_up_predictor_is_flagged_even_though_it_measured_fine(store) -> None:
    """The real numbers from this Lab's first run on AAPL: 475 predictions, zero failures,
    top of the Sharpe leaderboard at 1.58 — and it called UP on all 475 days."""
    experiment_id = _experiment(store, data_source="historical_bars", dataset="AAPL 2020..2025")
    store.record_model_run(
        experiment_id=experiment_id,
        result=_Result(
            model_name="elastic_net",
            total_predictions=475,
            metrics={
                "measured": True,
                "accuracy": 0.5495,
                "precision": 0.5495,
                "recall": 1.0,
                "win_rate": 0.5495,
                "sharpe_ratio": 1.5785,
                "information_coefficient": -0.1347,
                "total_predictions": 475,
                "true_positives": 261,
                "false_positives": 214,
                "false_negatives": 0,
            },
        ),
    )

    row = store.leaderboard("sharpe_ratio")["models"][0]

    assert row["model"] == "elastic_net", "it still ranks — the flag annotates, it does not filter"
    assert any("predicted UP on every sample" in r for r in row["degenerate"])
    assert any("ranks backwards" in r for r in row["degenerate"])


def test_an_always_down_predictor_is_flagged_too(store) -> None:
    metrics = {
        "measured": True,
        "recall": 0.0,
        "total_predictions": 300,
        "true_positives": 0,
        "false_positives": 0,
        "false_negatives": 140,
    }
    assert any("predicted DOWN on every sample" in r for r in store.degenerate_reasons(metrics))


def test_a_real_model_carries_no_degenerate_flags(store) -> None:
    """Break: flag on recall alone. A good model with high recall would then be marked degenerate."""
    metrics = {
        "measured": True,
        "recall": 0.92,
        "sharpe_ratio": 1.1,
        "information_coefficient": 0.08,
        "total_predictions": 400,
        "true_positives": 180,
        "false_positives": 90,
        "false_negatives": 16,
    }
    assert store.degenerate_reasons(metrics) == []


def test_an_unmeasured_result_is_not_also_called_degenerate(store) -> None:
    """It measured nothing, which metrics_measured already says. Two labels for one problem make
    the row read as two problems."""
    assert store.degenerate_reasons({"measured": False, "total_predictions": 0}) == []
