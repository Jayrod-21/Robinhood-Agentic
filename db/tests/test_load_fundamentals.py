"""Fundamentals ingest, against a real migrated Postgres.

Uses the same ephemeral-container pattern as test_loaders_db.py — never the live rh-db.

The mapping itself is unit-tested in tests/test_fmp.py against captured payloads. What is tested
HERE is everything that only shows up once a database is involved: that the two row kinds land with
the dating that makes them honest, that provenance and rows are atomic, that re-running appends an
observation rather than silently overwriting one, and that a symbol nobody has heard of is skipped
loudly instead of inventing an instrument.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
    from testcontainers.community.postgres import PostgresContainer
except ImportError:  # pragma: no cover
    from testcontainers.postgres import PostgresContainer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import load_fundamentals as lf
from migrate import main as migrate_main

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "fmp"
PG_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def fundamentals_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


def _admin_url(pg: PostgresContainer) -> str:
    return (
        f"postgresql://{pg.username}:{pg.password}"
        f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
    )


@pytest.fixture()
def db(fundamentals_pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    name = f"fund_{uuid.uuid4().hex[:12]}"
    admin = _admin_url(fundamentals_pg)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    monkeypatch.setenv("DATABASE_URL", url)
    assert migrate_main(["up", "--migrations-dir", str(REPO_MIGRATIONS)]) == 0
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def q(url: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _fixture(name: str):
    rows = json.loads((FIXTURES / f"{name}.json").read_text())
    return rows[0] if isinstance(rows, list) and rows else rows


@pytest.fixture()
def bundle() -> dict:
    return {
        "profile": _fixture("profile"),
        "ratios": _fixture("ratios"),
        "income": _fixture("income-statement"),
        "cash_flow": _fixture("cash-flow-statement"),
        "growth": _fixture("financial-growth"),
    }


@pytest.fixture()
def aapl(db: str) -> int:
    return q(db, "INSERT INTO securities (symbol) VALUES ('AAPL') RETURNING id")[0][0]


def _store(db_url: str, rows: list[dict], fetched_at: datetime) -> int:
    with psycopg.connect(db_url) as conn:
        return lf.store(conn, rows, fetched_at=fetched_at, notes="test run")


# ── the two row kinds ─────────────────────────────────────────────────────────────────────────


def test_statement_row_is_dated_by_acceptance_not_period_end(db, aapl, bundle):
    """The look-ahead guard, at the storage layer. The annual row must become visible only from
    the date the filing was accepted — five weeks after the period it describes."""
    row = lf.annual_row(bundle, aapl)
    _store(db, [row], datetime.now(timezone.utc))
    period_end, known_at = q(
        db, "SELECT period_end, known_at FROM fundamentals_snapshots WHERE period_type='annual'"
    )[0]
    assert str(period_end) == "2025-09-27"
    assert known_at.date().isoformat() == "2025-10-31"
    assert known_at.date() > period_end, "known_at must be AFTER the period it reports on"


def test_market_figures_never_land_on_the_statement_row(db, aapl, bundle):
    """A row carrying a past period's margins beside today's market cap would look point-in-time
    and be fiction. The split is the whole design; this pins it."""
    annual = lf.annual_row(bundle, aapl)
    assert annual["market_cap"] is None and annual["price"] is None
    assert annual["peg_ratio"] is None and annual["fcf_yield"] is None
    assert annual["gross_margin"] is not None, "statement figures DO belong here"


def test_snapshot_row_claims_only_the_moment_it_was_fetched(db, aapl, bundle):
    fetched = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    row = lf.snapshot_row(bundle, aapl, fetched)
    _store(db, [row], fetched)
    period_end, known_at, market_cap, fcf_yield = q(
        db,
        "SELECT period_end, known_at, market_cap, fcf_yield FROM fundamentals_snapshots "
        "WHERE period_type='snapshot'",
    )[0]
    assert period_end == fetched.date() and known_at == fetched
    assert market_cap > 0
    assert float(fcf_yield) > 1.0, "fcf_yield is a percent here, matching the screen spec"


def test_both_rows_from_one_fetch_coexist(db, aapl, bundle):
    fetched = datetime.now(timezone.utc)
    rows = [lf.annual_row(bundle, aapl), lf.snapshot_row(bundle, aapl, fetched)]
    assert _store(db, rows, fetched) == 2
    kinds = {r[0] for r in q(db, "SELECT period_type FROM fundamentals_snapshots")}
    assert kinds == {"annual", "snapshot"}


# ── provenance and atomicity ──────────────────────────────────────────────────────────────────


def test_provenance_row_is_written_with_the_data(db, aapl, bundle):
    fetched = datetime.now(timezone.utc)
    _store(db, [lf.annual_row(bundle, aapl)], fetched)
    provider, dataset, row_count = q(
        db, "SELECT provider, dataset, row_count FROM data_sources ORDER BY id DESC LIMIT 1"
    )[0]
    assert (provider, dataset, row_count) == ("fmp", "fundamentals_snapshots", 1)
    linked = q(db, "SELECT count(*) FROM fundamentals_snapshots WHERE source_id IS NOT NULL")[0][0]
    assert linked == 1, "every row must point at the provenance that produced it"


def test_a_failed_insert_leaves_no_orphan_provenance(db, aapl, bundle):
    """Provenance claiming rows that never landed is worse than no provenance: it is a record
    asserting something false. The loaders' convention is one transaction, and this proves it."""
    row = lf.annual_row(bundle, aapl)
    row["security_id"] = 999_999_999  # violates the securities FK
    with psycopg.connect(db) as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        lf.store(conn, [row], fetched_at=datetime.now(timezone.utc), notes="doomed")
    assert q(db, "SELECT count(*) FROM data_sources")[0][0] == 0
    assert q(db, "SELECT count(*) FROM fundamentals_snapshots")[0][0] == 0


def test_rerunning_on_an_unchanged_filing_does_not_mint_a_second_observation(db, aapl, bundle):
    """An observation is a FILING, not a fetch.

    This test used to assert the opposite — that two runs leave two rows — because uniqueness was
    keyed on source_id, which is minted per run. Under that key, re-reading an unchanged 10-K
    manufactured a new "belief" every time the loader ran, and nothing about what we believed had
    changed. In production NVDA ended up with three identical FY2026 rows that way.

    Migration 017 keys an observation on (security, period_end, period_type, known_at) instead, so
    the same filing read twice is one observation. What we believed and when is still answerable —
    see the restatement test below, which is where a second row genuinely belongs.
    """
    fetched = datetime.now(timezone.utc)
    _store(db, [lf.annual_row(bundle, aapl)], fetched)
    _store(db, [lf.annual_row(bundle, aapl)], fetched)
    count = q(db, "SELECT count(*) FROM fundamentals_snapshots WHERE period_type='annual'")[0][0]
    assert count == 1, "re-reading one filing is not two beliefs"


def test_a_restatement_appends_an_observation_rather_than_overwriting(db, aapl, bundle):
    """The invariant the test above used to stand for, tested through the thing that actually
    signals it: an amended filing arrives with a LATER acceptance date.

    Two rows, each dated by its own filing, so "what did we believe, and when" stays answerable —
    and so a backtest asking what was knowable in March gets the March figures, not the amendment
    published in August."""
    fetched = datetime.now(timezone.utc)
    original = lf.annual_row(bundle, aapl)
    _store(db, [original], fetched)

    amended = dict(original)
    amended["known_at"] = "2026-08-01T13:00:00Z"
    amended["revenue_ttm"] = (original["revenue_ttm"] or 0) + 1_000_000
    _store(db, [amended], fetched)

    rows = q(
        db,
        "SELECT known_at, revenue_ttm FROM fundamentals_snapshots"
        " WHERE period_type='annual' ORDER BY known_at",
    )
    assert len(rows) == 2, "an amended filing is a new observation, not an edit to the old one"
    assert rows[0][1] != rows[1][1], "the original figures must survive the restatement"


# ── refusals ──────────────────────────────────────────────────────────────────────────────────


def test_unknown_symbol_is_skipped_not_invented(db):
    """`securities` is reference data with its own loader. Creating rows here would let a typo'd
    ticker become a permanent instrument nothing else knows about."""
    with psycopg.connect(db) as conn:
        assert lf.resolve_security_id(conn, "NOSUCHTICKER") is None
    assert q(db, "SELECT count(*) FROM securities")[0][0] == 0


def test_symbol_lookup_is_case_insensitive(db, aapl):
    with psycopg.connect(db) as conn:
        assert lf.resolve_security_id(conn, "aapl") == aapl


def test_statement_without_an_acceptance_date_is_not_stored(db, aapl, bundle):
    """A row whose known_at cannot be established is invisible to point-in-time queries (the index
    excludes NULLs), so writing it would only inflate the row count with something unreadable."""
    stripped = dict(bundle["income"])
    stripped.pop("acceptedDate", None)
    stripped.pop("filingDate", None)
    sparse = {**bundle, "income": stripped, "cash_flow": {}}
    assert lf.annual_row(sparse, aapl) is None


def test_snapshot_without_market_cap_is_not_stored(db, aapl, bundle):
    sparse = {**bundle, "profile": {**bundle["profile"], "marketCap": None}}
    assert lf.snapshot_row(sparse, aapl, datetime.now(timezone.utc)) is None
