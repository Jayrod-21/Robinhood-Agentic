"""026's intraday log against a real throwaway Postgres.

Three guarantees, each of which fails silently if it regresses:

  * a ratio cannot be stored without the statement row it was computed from,
  * every row records which arithmetic produced it, so a corrected formula is applicable
    retroactively — the recovery path that did not exist when pe_forward was mapped from a PEG,
  * a gap in the series is attributable: the runs table separates "never ran", "ran and every quote
    failed", and "the market was closed".

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
def intraday_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


@pytest.fixture
def db(intraday_pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    name = f"intra_{uuid.uuid4().hex[:12]}"
    admin = (
        f"postgresql://{intraday_pg.username}:{intraday_pg.password}"
        f"@{intraday_pg.get_container_host_ip()}:{intraday_pg.get_exposed_port(5432)}/{intraday_pg.dbname}"
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


def _security(url: str, symbol: str = "NVDA") -> int:
    return q(
        url,
        "INSERT INTO securities (symbol, security_type) VALUES (%s,'stock') RETURNING id",
        (symbol,),
    )[0][0]


def _obs(url: str, security_id: int, **overrides) -> str:
    """Insert an observation. Returns "" on success, else the violated constraint name."""
    row = {
        "security_id": security_id,
        "observed_at": "2026-08-26 16:00:00+00",
        "session_date": "2026-08-26",
        "scope_reasons": ["held"],
        "price": 210.5,
        "formula_version": 1,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    try:
        q(url, f"INSERT INTO intraday_observations ({cols}) VALUES ({marks})", tuple(row.values()))
    except psycopg.errors.IntegrityError as exc:
        return getattr(exc.diag, "constraint_name", "") or str(exc)
    return ""


# ── a ratio cannot exist without its lineage ──────────────────────────────────────────────────


def test_a_ratio_without_a_statement_row_is_refused(db: str) -> None:
    """THE constraint. A ratio computed from no statement row was computed from nothing — the same
    defect class as the 0.5 the ML library used to return for "we could not measure this".

    Break: drop ck_intraday_obs_ratios_have_lineage. This goes red.
    """
    security_id = _security(db)
    assert "ck_intraday_obs_ratios_have_lineage" in _obs(db, security_id, pe_trailing=25.0)


def test_every_price_derived_ratio_is_covered_by_that_rule(db: str) -> None:
    security_id = _security(db)
    for column in ("pe_trailing", "pe_forward", "fcf_yield"):
        assert "ck_intraday_obs_ratios_have_lineage" in _obs(
            db, security_id, **{column: 1.5, "observed_at": f"2026-08-26 16:0{len(column)}:00+00"}
        ), column


def test_a_price_only_observation_is_perfectly_valid(db: str) -> None:
    """GLD and TMO landed exactly like this on the first real sweep: an ETF with no statement
    figures, and a security with no fundamentals row at all."""
    assert _obs(db, _security(db, "GLD")) == ""


# ── every row says how it was computed ────────────────────────────────────────────────────────


def test_formula_version_has_no_default(db: str) -> None:
    """A row that cannot say how it was computed cannot be corrected later, and correcting later is
    the entire point of the column."""
    security_id = _security(db)
    with psycopg.connect(db, autocommit=True) as conn, pytest.raises(psycopg.errors.NotNullViolation):
        conn.execute(
            "INSERT INTO intraday_observations"
            " (security_id, observed_at, session_date, scope_reasons, price)"
            " VALUES (%s, now(), current_date, ARRAY['held'], 1.0)",
            (security_id,),
        )


def test_rows_from_a_superseded_formula_can_be_found(db: str) -> None:
    """The query this whole design exists to make possible."""
    security_id = _security(db)
    _obs(db, security_id, formula_version=1, observed_at="2026-08-26 15:00:00+00")
    _obs(db, security_id, formula_version=2, observed_at="2026-08-26 15:30:00+00")

    stale = q(db, "SELECT count(*) FROM intraday_observations WHERE formula_version = 1")[0][0]
    assert stale == 1

    predicate = q(
        db,
        "SELECT count(*) FROM pg_class WHERE relname = 'ix_intraday_obs_formula'",
    )[0][0]
    assert predicate == 1, "and it is indexed, not a scan over the whole series"


# ── scope is recorded, so a gap is attributable ───────────────────────────────────────────────


def test_an_observation_must_say_why_it_was_collected(db: str) -> None:
    """Without it, a security vanishing from the series is ambiguous between "the collector
    stopped" and "it left the watchlist"."""
    security_id = _security(db)
    assert "ck_intraday_obs_scope" in _obs(db, security_id, scope_reasons=[])


def test_an_unknown_scope_reason_is_refused(db: str) -> None:
    assert "ck_intraday_obs_scope" in _obs(db, _security(db), scope_reasons=["vibes"])


def test_several_reasons_are_allowed(db: str) -> None:
    """Every security on the first real sweep carried both: debated AND held."""
    assert _obs(db, _security(db), scope_reasons=["debated", "held"]) == ""


def test_a_non_positive_price_is_refused(db: str) -> None:
    assert "ck_intraday_obs_price" in _obs(db, _security(db), price=0)


def test_one_observation_per_security_per_instant(db: str) -> None:
    """A re-run of the same 30-minute slot must update, not double the series."""
    security_id = _security(db)
    assert _obs(db, security_id) == ""
    assert "uq_intraday_obs" in _obs(db, security_id)


# ── the runs table separates three different silences ─────────────────────────────────────────


def _run(url: str, **overrides) -> str:
    row = {"session_date": "2026-08-26", "status": "complete", "completed_at": "now()",
           "scope_size": 15, "observed": 15, "failed": 0}
    row.update(overrides)
    literal = {"completed_at"}
    sets = ", ".join(row)
    marks = ", ".join(row[k] if k in literal else "%s" for k in row)
    params = tuple(v for k, v in row.items() if k not in literal)
    try:
        q(url, f"INSERT INTO intraday_collection_runs ({sets}) VALUES ({marks})", params)
    except psycopg.errors.IntegrityError as exc:
        return getattr(exc.diag, "constraint_name", "") or str(exc)
    return ""


def test_a_skipped_run_must_say_why(db: str) -> None:
    """"The market was closed" is the most common reason this table will hold a non-complete row,
    and a blank reason makes it indistinguishable from a crash."""
    assert "ck_intraday_runs_explained" in _run(db, status="skipped", error=None,
                                                scope_size=0, observed=0, failed=0)
    assert _run(db, status="skipped", error="market closed (no trading session on this date)",
                scope_size=0, observed=0, failed=0) == ""


def test_a_failed_run_must_say_why(db: str) -> None:
    assert "ck_intraday_runs_explained" in _run(db, status="failed", error="   ",
                                                observed=0, failed=15)


def test_a_running_sweep_cannot_carry_a_completion_time(db: str) -> None:
    assert "ck_intraday_runs_terminal" in _run(db, status="running", completed_at="now()")


def test_counts_cannot_exceed_the_scope(db: str) -> None:
    """A sweep reporting 20 observed of 15 in scope is arithmetic nobody can trust."""
    assert "ck_intraday_runs_counts" in _run(db, scope_size=15, observed=20, failed=0)


def test_an_empty_scope_is_a_real_outcome_not_a_failure(db: str) -> None:
    """Nothing held and nothing debated is a valid, reportable state."""
    assert _run(db, scope_size=0, observed=0, failed=0) == ""


def test_the_app_role_can_write_both_tables(db: str) -> None:
    for table in ("intraday_observations", "intraday_collection_runs"):
        privs = q(
            db,
            "SELECT has_table_privilege('rh_app',%s,'SELECT'), has_table_privilege('rh_app',%s,'INSERT'),"
            " has_table_privilege('rh_app',%s,'UPDATE'), has_table_privilege('rh_app',%s,'DELETE')",
            (table, table, table, table),
        )[0]
        assert privs[:3] == (True, True, True), table
        assert privs[3] is False, f"{table}: observations are superseded, never erased"


# ── 026 corrects 025's investable set ─────────────────────────────────────────────────────────


def test_share_classes_are_in_the_investable_index(db: str) -> None:
    """025 excluded them, and the first collector run caught it: 14 of 15 held names, missing BRK.B.
    The index predicate and instrument_class.INVESTABLE must agree, or the universe filter has two
    definitions — which is what putting it in one place was meant to prevent."""
    from instrument_class import INVESTABLE

    predicate = q(
        db,
        "SELECT pg_get_expr(indpred, indrelid) FROM pg_index i JOIN pg_class c"
        " ON c.oid = i.indexrelid WHERE c.relname = 'ix_securities_investable'",
    )[0][0]

    assert "share_class" in predicate
    for kind in INVESTABLE:
        assert f"'{kind}'" in predicate, kind
