"""025's instrument-type schema against a real throwaway Postgres.

Two guarantees, both of which fail silently if they regress:

  * `security_type` is CHECK-constrained to the classifier's vocabulary, so a typo'd 'stocks'
    cannot silently drop a company out of the investable universe.
  * `non_common_instrument` is a TERMINAL disposition, so verify_daily_series check 7 stops failing
    on 110 holes that were never mysteries — and keeps failing on the 6 that are.

Never touches the live rh-db — the container is ephemeral and dies with the session.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from instrument_class import INVESTABLE, TYPES

try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
    from testcontainers.community.postgres import PostgresContainer
except ImportError:  # pragma: no cover
    from testcontainers.postgres import PostgresContainer

from migrate import EXIT_OK
from migrate import main as migrate_main

REPO_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PG_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def types_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


@pytest.fixture
def db(types_pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    name = f"types_{uuid.uuid4().hex[:12]}"
    admin = (
        f"postgresql://{types_pg.username}:{types_pg.password}"
        f"@{types_pg.get_container_host_ip()}:{types_pg.get_exposed_port(5432)}/{types_pg.dbname}"
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


def _security(url: str, symbol: str, security_type: str | None = None) -> int:
    return q(
        url,
        "INSERT INTO securities (symbol, security_type) VALUES (%s, %s) RETURNING id",
        (symbol, security_type),
    )[0][0]


def _hole(url: str, security_id: int, symbol: str, disposition: str) -> str:
    """Insert a gap-audit row. Returns "" on success, else the violated constraint name."""
    try:
        q(
            url,
            "INSERT INTO price_gap_audit (security_id, symbol, gap_start, gap_resume, gap_days,"
            " missed_sessions, close_before, close_after, adj_ratio, disposition)"
            " VALUES (%s, %s, '2025-01-02', '2025-03-03', 60, 40, 10.0, 11.0, 1.1, %s)",
            (security_id, symbol, disposition),
        )
    except psycopg.errors.IntegrityError as exc:
        return getattr(exc.diag, "constraint_name", "") or str(exc)
    return ""


# ── the constrained vocabulary ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", TYPES)
def test_every_type_the_classifier_produces_is_accepted(db: str, kind: str) -> None:
    """A value the classifier can produce and the column will not accept is a loader that dies
    mid-run, halfway through 19,745 rows."""
    assert _security(db, f"SYM{kind[:3].upper()}", kind)


def test_a_typo_is_refused(db: str) -> None:
    """Break: drop ck_securities_security_type. 'stocks' then drops a company out of the investable
    universe with nothing to notice."""
    with psycopg.connect(db, autocommit=True) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO securities (symbol, security_type) VALUES ('TYPO', 'stocks')")


def test_null_stays_legal_and_means_never_classified(db: str) -> None:
    """A different fact from any of the seven values, and it must not be forced to impersonate one.
    The whole archive was NULL before this migration."""
    # A grammar-valid symbol: ck_securities_symbol caps the base at 10 characters.
    security_id = _security(db, "UNCLASSED", None)
    assert q(db, "SELECT security_type FROM securities WHERE id = %s", (security_id,))[0][0] is None


def test_the_column_comment_points_at_the_single_definition_of_investable(db: str) -> None:
    """Two definitions of "what a universe may draw from" is one edit away from a screen and a
    backtest disagreeing about whether warrants are in it."""
    comment = q(
        db,
        "SELECT col_description('securities'::regclass,"
        " (SELECT attnum FROM pg_attribute WHERE attrelid='securities'::regclass"
        "   AND attname='security_type'))",
    )[0][0]

    assert comment and "is_investable" in comment
    assert "issue #41" in comment


# ── the investable index ──────────────────────────────────────────────────────────────────────


def test_the_investable_index_covers_exactly_the_investable_types(db: str) -> None:
    predicate = q(
        db,
        "SELECT pg_get_expr(indpred, indrelid) FROM pg_index i JOIN pg_class c"
        " ON c.oid = i.indexrelid WHERE c.relname = 'ix_securities_investable'",
    )[0][0]

    for kind in INVESTABLE:
        assert f"'{kind}'" in predicate, f"{kind} is investable but not in the index predicate"
    for kind in set(TYPES) - set(INVESTABLE):
        assert f"'{kind}'" not in predicate, f"{kind} is not investable but is in the index"


def test_the_universe_query_returns_only_companies_and_funds(db: str) -> None:
    for symbol, kind in (
        ("AAPL", "stock"), ("SPY", "etf"), ("ACABW", "warrant"),
        ("EDTXU", "unit"), ("LFAE", "untracked"), ("NEW", None),
    ):
        _security(db, symbol, kind)

    universe = q(
        db, "SELECT symbol FROM securities WHERE security_type IN ('stock','etf') ORDER BY symbol"
    )
    assert [r[0] for r in universe] == ["AAPL", "SPY"]


# ── the new disposition ───────────────────────────────────────────────────────────────────────


def test_the_new_disposition_is_accepted(db: str) -> None:
    security_id = _security(db, "ACABW", "warrant")
    assert _hole(db, security_id, "ACABW", "non_common_instrument") == ""


def test_an_unknown_disposition_is_still_refused(db: str) -> None:
    """Widening the CHECK must not have loosened it to anything."""
    security_id = _security(db, "FOO", "stock")
    assert "ck_price_gap_audit_disposition" in _hole(db, security_id, "FOO", "made_up")


def test_every_pre_existing_disposition_still_works(db: str) -> None:
    """The migration DROPs and re-ADDs the constraint, which is where a value gets lost."""
    for i, disposition in enumerate((
        "pending_review", "identity_break", "provider_unresolvable", "split_missing",
        "halt_consistent", "continuity_confirmed", "spliced", "halt_accepted",
    )):
        security_id = _security(db, f"OLD{i}", "stock")
        assert _hole(db, security_id, f"OLD{i}", disposition) == "", disposition


def test_the_new_disposition_is_terminal_to_the_verifier(db: str) -> None:
    """check 7 fails while any NON-terminal disposition remains. If this value were left out of the
    terminal set, dispositioning 110 warrant holes would not turn the check green and the whole
    exercise would have moved rows without answering the question."""
    import load_delistings as ldel

    assert "non_common_instrument" in ldel.TERMINAL_DISPOSITIONS
    assert "non_common_instrument" not in ldel.NON_TERMINAL_DISPOSITIONS


def test_the_six_real_companies_stay_non_terminal(db: str) -> None:
    """DMN, DTC, KNW, OAS, ROCC and SNMP are common stock with real corporate events the provider
    has no history for. They must NOT be swept up — a residue for a human is the correct outcome,
    and clearing it would be tuning the alarm rather than answering it."""
    import load_delistings as ldel

    assert "provider_unresolvable" in ldel.NON_TERMINAL_DISPOSITIONS
