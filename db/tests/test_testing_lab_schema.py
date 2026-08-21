"""023_testing_lab against a real throwaway Postgres: the constraints that keep a fake result out.

The ML library these tables store came from Special-Sprinkle-Sauce carrying one defect (see #117,
#119): it answered "we could not measure this" with the number 0.5. Every fabricated value was
scrubbed from the library, but a library can be edited and a table cannot be un-written — so the
guarantees that matter are declared here, in the schema, where no future caller can skip them.

The three that carry the weight:

    ck_model_runs_measured_agrees   metrics_measured must EQUAL (predictions_made > failed).
                                    Both directions: a run cannot claim measurement it did not do,
                                    and cannot disclaim measurement it did.
    ux_model_runs_baseline          one baseline per model, so "the baseline" is never "whichever
                                    baseline row came back first".
    ck_sweeps_best_was_measured     no winner out of zero measured points — the sweep-shaped
                                    version of returning 0.5.

Never touches the live rh-db — the container is ephemeral and dies with the session.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
    from testcontainers.community.postgres import PostgresContainer
except ImportError:  # pragma: no cover
    from testcontainers.postgres import PostgresContainer

from migrate import EXIT_OK
from migrate import main as migrate_main

REPO_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PG_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def lab_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


@pytest.fixture
def db(lab_pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh, fully-migrated database per test, exported as DATABASE_URL for the runner."""
    name = f"lab_{uuid.uuid4().hex[:12]}"
    admin = (
        f"postgresql://{lab_pg.username}:{lab_pg.password}"
        f"@{lab_pg.get_container_host_ip()}:{lab_pg.get_exposed_port(5432)}/{lab_pg.dbname}"
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    monkeypatch.setenv("DATABASE_URL", url)
    assert migrate_main(["up", "--migrations-dir", str(REPO_MIGRATIONS)]) == EXIT_OK
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def q(url: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def refused(url: str, sql: str, params: tuple = ()) -> str:
    """Run a statement that must violate a constraint, and return the constraint's name."""
    with (
        psycopg.connect(url, autocommit=True) as conn,
        pytest.raises(psycopg.errors.IntegrityError) as exc,
    ):
        conn.execute(sql, params)
    return getattr(exc.value.diag, "constraint_name", "") or str(exc.value)


def experiment(url: str, **overrides) -> int:
    """Insert a runnable experiment and return its id."""
    row = {
        "name": "nightly xgboost",
        "kind": "walk_forward",
        "data_source": "synthetic",
        "dataset": "seed=42",
        "validation_kind": "walk_forward",
        "status": "running",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    return q(
        url, f"INSERT INTO experiments ({cols}) VALUES ({marks}) RETURNING id", tuple(row.values())
    )[0][0]


def run_row(url: str, experiment_id: int, **overrides) -> str:
    """Attempt a model_runs insert. Returns "" on success, else the violated constraint name."""
    row = {
        "experiment_id": experiment_id,
        "model_name": "xgboost",
        "metrics_measured": True,
        "predictions_made": 200,
        "predictions_failed": 0,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    sql = f"INSERT INTO model_runs ({cols}) VALUES ({marks})"
    try:
        q(url, sql, tuple(row.values()))
    except psycopg.errors.IntegrityError as exc:
        return getattr(exc.diag, "constraint_name", "") or str(exc)
    return ""


# ── experiments: a run declares its data and explains its failure ─────────────────────────────


def test_an_experiment_must_declare_what_data_backed_it(db: str) -> None:
    """No default on data_source, deliberately. A synthetic run that sorts into the same
    leaderboard as one on real history is the most expensive lie these tables could tell."""
    with psycopg.connect(db, autocommit=True) as conn, pytest.raises(psycopg.errors.NotNullViolation):
        conn.execute(
            "INSERT INTO experiments (name, kind, dataset, validation_kind)"
            " VALUES ('x', 'train', 'seed=1', 'none')"
        )


def test_data_source_is_restricted_to_what_the_lab_can_actually_read(db: str) -> None:
    assert "ck_experiments_data_source" in refused(
        db,
        "INSERT INTO experiments (name, kind, data_source, dataset, validation_kind)"
        " VALUES ('x', 'train', 'vibes', 'seed=1', 'none')",
    )


def test_a_running_experiment_cannot_carry_a_completion_time(db: str) -> None:
    """Break: drop ck_experiments_terminal. A crashed run then looks exactly like a live one."""
    assert "ck_experiments_terminal" in refused(
        db,
        "INSERT INTO experiments (name, kind, data_source, dataset, validation_kind, status,"
        " completed_at) VALUES ('x', 'train', 'synthetic', 's', 'none', 'running', now())",
    )


def test_a_finished_experiment_must_say_when(db: str) -> None:
    assert "ck_experiments_terminal" in refused(
        db,
        "INSERT INTO experiments (name, kind, data_source, dataset, validation_kind, status)"
        " VALUES ('x', 'train', 'synthetic', 's', 'none', 'complete')",
    )


def test_a_failed_experiment_must_explain_itself(db: str) -> None:
    """A blank error on a failed row makes a real failure indistinguishable from an ignored run."""
    assert "ck_experiments_failure_explained" in refused(
        db,
        "INSERT INTO experiments (name, kind, data_source, dataset, validation_kind, status,"
        " completed_at, error) VALUES ('x', 'train', 'synthetic', 's', 'none', 'failed', now(), '  ')",
    )

    q(
        db,
        "INSERT INTO experiments (name, kind, data_source, dataset, validation_kind, status,"
        " completed_at, error) VALUES ('x', 'train', 'synthetic', 's', 'none', 'failed', now(),"
        " 'xgboost not installed in the lab image')",
    )


# ── model_runs: the constraint that keeps a fabricated metric out ─────────────────────────────


def test_a_run_cannot_claim_measurement_it_did_not_do(db: str) -> None:
    """Every prediction failed, yet the row says it measured something. This is the exact shape of
    the ported defect: nothing to score, a number reported anyway."""
    exp = experiment(db)
    assert "ck_model_runs_measured_agrees" in run_row(
        db, exp, metrics_measured=True, predictions_made=200, predictions_failed=200
    )


def test_a_run_cannot_disclaim_measurement_it_did_do(db: str) -> None:
    """The other direction, which matters just as much: a real result flagged unmeasured drops
    silently out of every leaderboard that reads the partial index."""
    exp = experiment(db)
    assert "ck_model_runs_measured_agrees" in run_row(
        db, exp, metrics_measured=False, predictions_made=200, predictions_failed=3
    )


def test_a_run_that_measured_nothing_is_still_storable(db: str) -> None:
    """A total failure is a result. It must be keepable — it just must not rank."""
    exp = experiment(db)
    assert run_row(
        db, exp, metrics_measured=False, predictions_made=200, predictions_failed=200
    ) == ""


def test_more_failures_than_predictions_is_impossible(db: str) -> None:
    exp = experiment(db)
    assert "ck_model_runs_counts" in run_row(
        db, exp, metrics_measured=True, predictions_made=10, predictions_failed=11
    )


def test_the_leaderboard_index_excludes_unmeasured_runs_by_construction(db: str) -> None:
    """The ranking path reads a partial index, so a query that forgets its WHERE clause still
    cannot surface a run that measured nothing."""
    exp = experiment(db)
    run_row(db, exp, model_name="xgboost", predictions_made=200, predictions_failed=1)
    run_row(
        db,
        exp,
        model_name="arima",
        metrics_measured=False,
        predictions_made=200,
        predictions_failed=200,
    )

    predicate = q(
        db,
        "SELECT pg_get_expr(indpred, indrelid) FROM pg_index i"
        " JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'ix_model_runs_rankable'",
    )
    assert predicate and "metrics_measured" in predicate[0][0]

    rankable = q(db, "SELECT model_name FROM model_runs WHERE metrics_measured")
    assert [r[0] for r in rankable] == ["xgboost"]


def test_a_model_has_at_most_one_baseline(db: str) -> None:
    """Break: drop ux_model_runs_baseline. "The baseline" becomes whichever row sorts first."""
    exp = experiment(db)
    assert run_row(db, exp, is_baseline=True) == ""
    assert "ux_model_runs_baseline" in run_row(db, exp, is_baseline=True)

    # A different model may hold its own baseline — the uniqueness is per model, not global.
    assert run_row(db, exp, model_name="elastic_net", is_baseline=True) == ""


def test_a_baseline_must_have_measured_something(db: str) -> None:
    """Everything in the Lab is compared against the baseline. An unmeasured one voids all of it."""
    exp = experiment(db)
    assert "ck_model_runs_baseline_measured" in run_row(
        db,
        exp,
        is_baseline=True,
        metrics_measured=False,
        predictions_made=50,
        predictions_failed=50,
    )


def test_runs_die_with_their_experiment(db: str) -> None:
    """ON DELETE CASCADE: an orphaned run has no data_source, so it can never be interpreted."""
    exp = experiment(db)
    run_row(db, exp)
    q(db, "DELETE FROM experiments WHERE id = %s", (exp,))
    assert q(db, "SELECT count(*) FROM model_runs")[0][0] == 0


# ── sweeps: no winner out of nothing ──────────────────────────────────────────────────────────


def _sweep(db: str, **overrides) -> str:
    row = {
        "experiment_id": experiment(db, kind="sweep"),
        "param": "max_depth",
        "metric": "sharpe_ratio",
        "points_tested": 5,
        "points_measured": 5,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    try:
        q(db, f"INSERT INTO sweeps ({cols}) VALUES ({marks})", tuple(row.values()))
    except psycopg.errors.IntegrityError as exc:
        return getattr(exc.diag, "constraint_name", "") or str(exc)
    return ""


def test_a_sweep_cannot_report_a_winner_it_never_measured(db: str) -> None:
    assert "ck_sweeps_best_was_measured" in _sweep(
        db, points_tested=5, points_measured=0, best_value=6, best_metric_value=1.4
    )


def test_a_sweep_that_measured_nothing_is_storable_without_a_winner(db: str) -> None:
    """Five points tested, none measurable. Worth recording, with no peak claimed."""
    assert _sweep(db, points_tested=5, points_measured=0) == ""


def test_a_sweep_cannot_measure_more_points_than_it_tested(db: str) -> None:
    assert "ck_sweeps_counts" in _sweep(db, points_tested=3, points_measured=4)


# ── grants: the Lab writes its own tables and nothing on the order path ───────────────────────


def test_the_app_role_can_read_and_write_the_lab_tables(db: str) -> None:
    for table in ("experiments", "model_runs", "sweeps"):
        privs = q(
            db,
            "SELECT has_table_privilege('rh_app', %s, 'SELECT'),"
            " has_table_privilege('rh_app', %s, 'INSERT'),"
            " has_table_privilege('rh_app', %s, 'UPDATE'),"
            " has_table_privilege('rh_app', %s, 'DELETE')",
            (table, table, table, table),
        )[0]
        assert privs[:3] == (True, True, True), table
        # No DELETE. Results are corrected by superseding them, not by erasing the record of what
        # was measured — the same append-only reasoning the auth events table is built on.
        assert privs[3] is False, table
