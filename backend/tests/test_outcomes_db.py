"""Integration: the outcome loop against a REAL Postgres running the REAL repo migrations,
connected as the REAL runtime role.

This is the test the fakes cannot give us: migrations 001-011 applied by db/migrate.py, rh_app's
actual grants in force, and the whole entry-thesis → exit → realized P&L → lesson path exercised
through the app's own pool. It also proves the append-only posture from the app's side of the
socket: rh_app really cannot rewrite a knowledge-base row or a lot's entry columns.

Never touches the live rh-db — the container here is ephemeral and dies with the session
(same pattern as db/tests/test_runner_db.py).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from app.db import close_pool, connection, db_health, reset_db_settings
from app.services import outcomes

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "db"))  # migrate.py is a top-level script in db/


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _testcontainers_available() -> bool:
    """Docker and the testcontainers PACKAGE are separate preconditions.

    Guarding on Docker alone was not enough: CI's backend job runs on a GitHub runner that HAS a
    Docker daemon but installs only backend/requirements.txt, which does not include
    testcontainers — that lives in the database job. So the guarded block executed, both import
    paths raised ModuleNotFoundError, and collection died before any skip could apply.
    """
    return importlib.util.find_spec("testcontainers") is not None


_INTEGRATION_READY = _docker_available() and _testcontainers_available()

pytestmark = pytest.mark.skipif(
    not _INTEGRATION_READY,
    reason="integration test needs docker AND the testcontainers package",
)

if _INTEGRATION_READY:  # imports guarded so collection succeeds without either precondition
    import psycopg

    try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        from testcontainers.postgres import PostgresContainer

    from migrate import EXIT_OK
    from migrate import main as migrate_main

PG_IMAGE = "postgres:16-alpine"  # same major the live stack pins by digest
APP_PASSWORD = "test-rh-app-pw"

ENTRY_DATE = date(2026, 6, 3)
ENTRY_PRICE = Decimal("440.79")
SHARES = Decimal("0.04991")


@pytest.fixture(scope="module")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


@pytest.fixture(scope="module")
def urls(pg_container: PostgresContainer) -> Iterator[dict[str, str]]:
    """One migrated database for the module: admin URL (superuser) + app URL (rh_app)."""
    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    admin_root = f"postgresql://{pg_container.username}:{pg_container.password}@{host}:{port}"
    name = f"tdb_{uuid.uuid4().hex[:12]}"

    with psycopg.connect(f"{admin_root}/{pg_container.dbname}", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    admin_url = f"{admin_root}/{name}"

    # Apply the REAL migrations with the REAL runner (it reads DATABASE_URL).
    import os

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = admin_url
    try:
        assert migrate_main(["up"]) == EXIT_OK
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old

    # 001 ships rh_app with LOGIN and no password; give it one so the app pool can authenticate.
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f"ALTER ROLE rh_app WITH PASSWORD '{APP_PASSWORD}'")

    yield {
        "admin": admin_url,
        "app": f"postgresql://rh_app:{APP_PASSWORD}@{host}:{port}/{name}",
    }


@pytest.fixture(scope="module")
def seeded(urls) -> dict[str, int]:
    """Reference rows the outcome loop hangs off: a security, the 'real' agent, its book, a lot."""
    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        sec_id = conn.execute(
            "INSERT INTO securities (symbol, name) VALUES ('TSM', 'Taiwan Semiconductor') RETURNING id"
        ).fetchone()[0]
        sec2_id = conn.execute(
            "INSERT INTO securities (symbol, name) VALUES ('VST', 'Vistra') RETURNING id"
        ).fetchone()[0]
        agent_id = conn.execute(
            "INSERT INTO agents (agent_key, version, kind, display_name) "
            "VALUES ('real', 1, 'real', 'Live account') RETURNING id"
        ).fetchone()[0]
        portfolio_id = conn.execute(
            "INSERT INTO paper_portfolios (kind, agent_id, inception_date, base_value, cash) "
            "VALUES ('real', %s, %s, 100.00, 12.00) RETURNING id",
            (agent_id, ENTRY_DATE),
        ).fetchone()[0]
        for sid in (sec_id, sec2_id):
            conn.execute(
                "INSERT INTO paper_portfolio_positions "
                "(portfolio_id, security_id, entry_date, shares, entry_price) "
                "VALUES (%s, %s, %s, %s, %s)",
                (portfolio_id, sid, ENTRY_DATE, SHARES, ENTRY_PRICE),
            )
    return {"security_id": sec_id, "agent_id": agent_id, "portfolio_id": portfolio_id}


@pytest.fixture(autouse=True)
def app_db_state(urls, monkeypatch):
    """Point the app's pool at the migrated database AS rh_app — grants in full force."""
    monkeypatch.setenv("DATABASE_URL", urls["app"])
    reset_db_settings()
    close_pool()
    yield
    close_pool()
    reset_db_settings()


def test_db_health_reports_reachable_as_rh_app(seeded):
    report = db_health()
    assert report["configured"] is True
    assert report["reachable"] is True
    assert report["role"] == "rh_app"
    # schema_migrations is deliberately not granted to rh_app; health must degrade that field,
    # not fail the probe.
    assert report["schema_version"] is not None
    assert report["error"] is None


def test_full_outcome_loop_thesis_exit_lesson(seeded, urls):
    thesis_id = outcomes.record_entry_thesis(
        symbol="TSM",
        title="TSM entry",
        thesis="Compute anchor: capacity sold out through 2028, FCF yield 31.5%.",
        portfolio_id=seeded["portfolio_id"],
        agent_id=seeded["agent_id"],
    )
    assert thesis_id > 0

    record = outcomes.record_exit(
        portfolio_id=seeded["portfolio_id"],
        symbol="TSM",
        entry_date=ENTRY_DATE,
        exit_date=date(2026, 8, 13),
        exit_price=Decimal("455.00"),
        exit_reason="thesis played out; exiting into the post-earnings gap",
        lesson="dated catalysts resolve fast; take the gap",
        agent_id=seeded["agent_id"],
    )
    # (455.00 - 440.79) * 0.04991 = 0.7092... — the SERVER rounded to cents, and what came back
    # is what the table now holds.
    assert record.realized_pnl == Decimal("0.71")
    assert record.shares == SHARES
    assert record.entry_price == ENTRY_PRICE

    lesson_id = outcomes.record_lesson(
        title="Catalyst exits",
        lesson="Never hold an uncapped binary through resolution.",
        symbol="TSM",
        portfolio_id=seeded["portfolio_id"],
        agent_id=seeded["agent_id"],
    )
    corrected_id = outcomes.record_lesson(
        title="Catalyst exits (corrected)",
        lesson="Never hold an uncapped binary through resolution; size the tail, don't ban it.",
        supersedes_id=lesson_id,
    )
    assert corrected_id > lesson_id

    # The durable state, read back from Postgres rather than trusted from return values.
    with psycopg.connect(urls["admin"]) as conn:
        exit_row = conn.execute(
            "SELECT exit_date, exit_price, exit_reason, realized_pnl "
            "FROM paper_portfolio_positions WHERE portfolio_id = %s AND security_id = %s",
            (seeded["portfolio_id"], seeded["security_id"]),
        ).fetchone()
        assert exit_row == (
            date(2026, 8, 13),
            Decimal("455.000000"),
            "thesis played out; exiting into the post-earnings gap",
            Decimal("0.71"),
        )
        kinds = [
            r[0]
            for r in conn.execute(
                "SELECT entry_type FROM knowledge_base_entries ORDER BY id"
            ).fetchall()
        ]
        assert kinds == ["thesis", "outcome", "lesson", "lesson"]

    # And the read path future debates use.
    entries = outcomes.recent_entries(symbol="TSM")
    assert {e["entry_type"] for e in entries} >= {"thesis", "outcome", "lesson"}
    outcome_entry = next(e for e in entries if e["entry_type"] == "outcome")
    assert "455.00" in outcome_entry["body"]
    assert outcome_entry["lesson"] == "dated catalysts resolve fast; take the gap"


def test_double_exit_refused_against_real_schema(seeded):
    with pytest.raises(outcomes.OutcomeError, match=r"already\s+exited"):
        outcomes.record_exit(
            portfolio_id=seeded["portfolio_id"],
            symbol="TSM",
            entry_date=ENTRY_DATE,
            exit_date=date(2026, 8, 14),
            exit_price=Decimal("460.00"),
            exit_reason="second exit must be impossible",
        )


def test_unknown_symbol_refused_against_real_schema(seeded):
    with pytest.raises(outcomes.OutcomeError, match="no live row in securities"):
        outcomes.record_entry_thesis(symbol="ZZZZ", title="t", thesis="x")


def test_rh_app_really_cannot_rewrite_history(seeded):
    """The append-only REVOKEs (004/011), proven from the app's side of the socket."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connection() as conn:
            conn.execute("UPDATE knowledge_base_entries SET title = 'rewritten' WHERE id = 1")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connection() as conn:
            conn.execute(
                "UPDATE paper_portfolio_positions SET entry_price = 1 WHERE portfolio_id = %s",
                (seeded["portfolio_id"],),
            )

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connection() as conn:
            conn.execute("DELETE FROM knowledge_base_entries WHERE id = 1")


def test_exit_via_api_end_to_end(seeded, urls):
    """The router path: POST an exit for the second lot, read history back, as rh_app."""
    from app.main import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app())

    res = client.post(
        "/api/history/exits",
        json={
            "portfolio_id": seeded["portfolio_id"],
            "symbol": "VST",
            "entry_date": ENTRY_DATE.isoformat(),
            "exit_date": "2026-08-13",
            "exit_price": "500.00",
            "exit_reason": "power thesis exhausted",
        },
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    # (500.00 - 440.79) * 0.04991 = 2.9552... → 2.96, computed server-side in NUMERIC.
    assert body["realized_pnl"] == "2.96"

    res = client.get("/api/history/entries", params={"symbol": "VST", "entry_type": "outcome"})
    assert res.status_code == 200
    entries = res.json()["entries"]
    assert len(entries) == 1
    assert "power thesis exhausted" in entries[0]["title"]

    res = client.get("/api/db/health")
    assert res.status_code == 200
    assert res.json()["reachable"] is True
