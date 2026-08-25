"""024's reconciliation columns on cycle_runs: the difference between "fine" and "never looked".

The cycle ran twice a day for weeks against a slate that no longer described the book, and every
one of those runs looked exactly like a healthy one. These constraints exist so the database cannot
store that ambiguity again.

Never touches the live rh-db — the container is ephemeral and dies with the session.
"""

from __future__ import annotations

import json
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
def recon_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


@pytest.fixture
def db(recon_pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    name = f"recon_{uuid.uuid4().hex[:12]}"
    admin = (
        f"postgresql://{recon_pg.username}:{recon_pg.password}"
        f"@{recon_pg.get_container_host_ip()}:{recon_pg.get_exposed_port(5432)}/{recon_pg.dbname}"
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


def _run(url: str) -> int:
    return q(url, "INSERT INTO cycle_runs (phase) VALUES ('open') RETURNING id")[0][0]


def _reconcile(url: str, run_id: int, **cols) -> str:
    """Patch the reconciliation columns. Returns "" on success, else the violated constraint."""
    row = {
        "reconciled_at": "now()",
        "in_sync": True,
        "recon_matched": 8,
        "recon_drifted": 0,
        "recon_missing": 0,
        "recon_unexpected": 0,
        "recon_breaches": 0,
    }
    row.update(cols)
    literal = {"reconciled_at"}
    sets = ", ".join(f"{k} = {v}" if k in literal else f"{k} = %s" for k, v in row.items())
    params = tuple(v for k, v in row.items() if k not in literal)
    try:
        q(url, f"UPDATE cycle_runs SET {sets} WHERE id = %s", (*params, run_id))
    except psycopg.errors.IntegrityError as exc:
        return getattr(exc.diag, "constraint_name", "") or str(exc)
    return ""


# ── never looked vs looked and it was fine ────────────────────────────────────────────────────


def test_a_run_that_never_reconciled_leaves_every_column_null(db: str) -> None:
    """NULL is the honest value. Zeros would say the check ran and found nothing wrong."""
    run_id = _run(db)
    row = q(
        db,
        "SELECT reconciled_at, in_sync, recon_matched FROM cycle_runs WHERE id = %s",
        (run_id,),
    )[0]

    assert row == (None, None, None)


def test_a_partial_reconciliation_is_refused(db: str) -> None:
    """Break: drop ck_cycle_runs_recon_ran. A row could then say in_sync without a timestamp, and
    "we never looked" becomes indistinguishable from "we looked and it was fine"."""
    run_id = _run(db)
    assert "ck_cycle_runs_recon_ran" in _reconcile(db, run_id, reconciled_at="NULL")


def test_a_timestamp_without_a_verdict_is_refused(db: str) -> None:
    run_id = _run(db)
    assert "ck_cycle_runs_recon_ran" in _reconcile(db, run_id, in_sync=None, recon_matched=None)


# ── in_sync must agree with the counts ────────────────────────────────────────────────────────


def test_a_run_cannot_claim_to_be_in_sync_while_holding_an_undocumented_position(db: str) -> None:
    """The live book on 2026-08-25 held ten of these. A row claiming in_sync beside them is the
    written-claim-that-means-something-else defect in a column."""
    run_id = _run(db)
    assert "ck_cycle_runs_recon_agrees" in _reconcile(
        db, run_id, in_sync=True, recon_unexpected=10
    )


def test_a_run_cannot_claim_to_be_in_sync_through_a_guardrail_breach(db: str) -> None:
    run_id = _run(db)
    assert "ck_cycle_runs_recon_agrees" in _reconcile(db, run_id, in_sync=True, recon_breaches=1)


def test_a_run_cannot_claim_desync_with_nothing_wrong(db: str) -> None:
    """The other direction: a false alarm recorded as fact trains an operator to ignore the alarm."""
    run_id = _run(db)
    assert "ck_cycle_runs_recon_agrees" in _reconcile(db, run_id, in_sync=False)


def test_the_real_2026_08_25_verdict_stores_cleanly(db: str) -> None:
    """0 matched, 5 drifted, 3 missing, 10 undocumented, 2 breaches — the run that made this
    change necessary."""
    run_id = _run(db)
    findings = json.dumps(
        [
            {"kind": "missing", "symbol": "TSM", "target_weight_pct": 22.0},
            {"kind": "unexpected", "symbol": "SVRA", "live_weight_pct": 0.52},
            {"kind": "breach", "rule": "Cash 10-20% band", "detail": "cash is 92.5%"},
        ]
    )
    assert (
        _reconcile(
            db,
            run_id,
            in_sync=False,
            recon_matched=0,
            recon_drifted=5,
            recon_missing=3,
            recon_unexpected=10,
            recon_breaches=2,
        )
        == ""
    )
    q(db, "UPDATE cycle_runs SET recon_findings = %s WHERE id = %s", (findings, run_id))

    stored = q(db, "SELECT in_sync, recon_findings FROM cycle_runs WHERE id = %s", (run_id,))[0]
    assert stored[0] is False
    assert {f["kind"] for f in stored[1]} == {"missing", "unexpected", "breach"}


def test_negative_counts_are_refused(db: str) -> None:
    """in_sync=False too, so the agrees constraint is satisfied and the COUNTS check is the one
    under test — otherwise this passes for the wrong reason and stops covering anything."""
    run_id = _run(db)
    assert "ck_cycle_runs_recon_counts" in _reconcile(
        db, run_id, in_sync=False, recon_missing=-1
    )


# ── the query an operator actually runs ───────────────────────────────────────────────────────


def test_desynced_runs_are_indexed_for_the_question_worth_asking(db: str) -> None:
    """"When did this last go wrong, and is it wrong now" — a partial index, so the scan is over
    the failures rather than over every run ever."""
    predicate = q(
        db,
        "SELECT pg_get_expr(indpred, indrelid) FROM pg_index i"
        " JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'ix_cycle_runs_desync'",
    )
    assert predicate and "in_sync" in predicate[0][0]

    healthy, broken, never = _run(db), _run(db), _run(db)
    _reconcile(db, healthy, in_sync=True)
    _reconcile(db, broken, in_sync=False, recon_matched=0, recon_missing=3)

    found = [r[0] for r in q(db, "SELECT id FROM cycle_runs WHERE in_sync IS FALSE ORDER BY id")]
    assert found == [broken]
    assert never not in found, "a run that never checked is not a run that failed"


def test_the_app_role_can_write_the_new_columns(db: str) -> None:
    """cycle_state patches them from the backend, which runs as rh_app."""
    assert q(db, "SELECT has_table_privilege('rh_app', 'cycle_runs', 'UPDATE')")[0][0] is True
