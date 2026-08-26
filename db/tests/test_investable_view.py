"""027's investable_securities view, and the drift it exists to prevent.

#41 declared db/instrument_class.py::is_investable "the single definition of what a universe may
draw from". It could not be, for anything unable to import it — the Testing Lab's image ships lab/
and src/ and deliberately not db/, so its only option was to hardcode the type list in a query.

Two definitions of a universe filter is how one of them drifts, and #135 showed the bill: 025's
index predicate and the classifier agreed with each other and were both wrong about share classes,
so BRK.B — a held position — silently left the universe and the Testing Lab's scope came back 14
of 15.

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
def view_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


@pytest.fixture
def db(view_pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    name = f"view_{uuid.uuid4().hex[:12]}"
    admin = (
        f"postgresql://{view_pg.username}:{view_pg.password}"
        f"@{view_pg.get_container_host_ip()}:{view_pg.get_exposed_port(5432)}/{view_pg.dbname}"
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


@pytest.fixture
def populated(db: str) -> str:
    """One security of every classified type, plus one that was never classified."""
    for i, kind in enumerate(TYPES):
        q(db, "INSERT INTO securities (symbol, security_type) VALUES (%s,%s)", (f"SYM{i}", kind))
    q(db, "INSERT INTO securities (symbol, security_type) VALUES ('UNCLASSED', NULL)")
    return db


# ── the view and the classifier must agree ────────────────────────────────────────────────────


def test_the_view_admits_exactly_what_the_classifier_calls_investable(populated: str) -> None:
    """THE test in this file. Break: change INVESTABLE without changing the view, or the reverse."""
    in_view = {
        r[0] for r in q(populated, "SELECT security_type FROM investable_securities")
    }

    assert in_view == set(INVESTABLE), (
        f"the view admits {sorted(in_view)} but instrument_class.INVESTABLE is "
        f"{sorted(INVESTABLE)} — a universe filter with two definitions"
    )


def test_share_classes_are_in_it(populated: str) -> None:
    """Named explicitly because leaving them out is the mistake that has already been made once,
    and it cost a held position (BRK.B) its place in the Testing Lab's scope."""
    kinds = {r[0] for r in q(populated, "SELECT security_type FROM investable_securities")}
    assert "share_class" in kinds


def test_warrants_units_rights_and_untracked_are_excluded(populated: str) -> None:
    kinds = {r[0] for r in q(populated, "SELECT security_type FROM investable_securities")}
    for kind in ("warrant", "unit", "right", "untracked"):
        assert kind not in kinds, kind


def test_an_unclassified_security_is_excluded(populated: str) -> None:
    """NULL is not "probably fine". It means the classifier has not run for that row, and
    defaulting the unknown to investable is how a warrant ends up being fundamentally screened."""
    symbols = {r[0] for r in q(populated, "SELECT symbol FROM investable_securities")}
    assert "UNCLASSED" not in symbols


# ── it is usable as a universe ────────────────────────────────────────────────────────────────


def test_the_view_carries_the_columns_a_consumer_needs(populated: str) -> None:
    """The Lab joins it to price_bars_daily on id and reports symbol and security_type."""
    columns = {
        r[0]
        for r in q(
            populated,
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'investable_securities'",
        )
    }
    assert {"id", "symbol", "security_type", "name"} <= columns


def test_the_app_role_can_read_it(populated: str) -> None:
    """The backend and the Lab both run as rh_app; a view they cannot select from is not a shared
    definition, it is a broken one."""
    assert q(populated, "SELECT has_table_privilege('rh_app','investable_securities','SELECT')")[0][0]


def test_the_view_documents_where_its_definition_is_mirrored(populated: str) -> None:
    comment = q(
        populated, "SELECT obj_description('investable_securities'::regclass, 'pg_class')"
    )[0][0]

    assert comment and "instrument_class" in comment
    assert "test_investable_view" in comment, "and it names the test that pins the two together"
