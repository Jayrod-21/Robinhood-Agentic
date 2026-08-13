"""app.db connection layer: graceful degradation is THE contract (issue #6).

The database has no host port (ADR-001) and the dashboard containers are not on its network, so
"no DB" and "unreachable DB" are normal operating states, not edge cases. These tests pin that:
nothing blocks, nothing crashes, every failure is a typed DbUnavailable within a bounded time,
and the health report never leaks the DSN or password.
"""

from __future__ import annotations

import time

import pytest
from app import db as app_db
from app.db import DbUnavailable, close_pool, connection, db_health, get_db_settings, reset_db_settings
from app.db.pool import get_pool

# A loopback port nothing listens on: connect fails fast with ECONNREFUSED, which exercises the
# pool's retry/timeout path without waiting on a routing black hole.
UNREACHABLE_DSN = "postgresql://rh_app:sekritpw@127.0.0.1:9/rh"


@pytest.fixture(autouse=True)
def clean_db_state(monkeypatch):
    """Each test starts DB-less and with fresh settings; pools never leak between tests."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_db_settings()
    close_pool()
    yield
    close_pool()
    reset_db_settings()


def _configure_unreachable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DSN)
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("DB_ACQUIRE_TIMEOUT_SECONDS", "0.5")
    reset_db_settings()


# ── not configured ───────────────────────────────────────────────────────────────────────────


def test_no_dsn_means_no_pool():
    assert get_db_settings().database_url is None
    assert get_pool() is None


def test_empty_dsn_treated_as_absent(monkeypatch):
    # docker-compose passes ${DATABASE_URL:-} == "" when unset; that must read as "no database".
    monkeypatch.setenv("DATABASE_URL", "   ")
    reset_db_settings()
    assert get_db_settings().database_url is None
    assert get_pool() is None


def test_connection_without_dsn_raises_typed_unavailable():
    with pytest.raises(DbUnavailable) as excinfo:
        with connection():
            pytest.fail("must not yield a connection with no DSN configured")
    assert excinfo.value.reason == "not_configured"
    assert "DATABASE_URL" in str(excinfo.value)


def test_health_without_dsn_reports_not_configured():
    report = db_health()
    assert report["configured"] is False
    assert report["reachable"] is False
    assert report["error"] == "DATABASE_URL is not set"


# ── configured but unreachable (the ADR-001 network reality) ─────────────────────────────────


def test_unreachable_db_fails_fast_and_typed(monkeypatch):
    _configure_unreachable(monkeypatch)
    started = time.monotonic()
    with pytest.raises(DbUnavailable) as excinfo:
        with connection():
            pytest.fail("must not yield a connection when the DB is unreachable")
    elapsed = time.monotonic() - started
    assert excinfo.value.reason == "unreachable"
    # Bounded: the acquire timeout is 0.5s; anything near a default network timeout means the
    # request handler would have hung and the dashboard page with it.
    assert elapsed < 5.0, f"degradation took {elapsed:.1f}s — a hung page, not a graceful failure"


def test_unreachable_db_health_answers_and_leaks_nothing(monkeypatch):
    _configure_unreachable(monkeypatch)
    report = db_health()
    assert report["configured"] is True
    assert report["reachable"] is False
    assert report["error"], "an unreachable DB must be reported, not silently blank"
    # The DSN carries the rh_app password; no field of the health report may contain it.
    blob = repr(report)
    assert "sekritpw" not in blob
    assert UNREACHABLE_DSN not in blob


def test_db_unavailable_messages_never_contain_the_dsn(monkeypatch):
    _configure_unreachable(monkeypatch)
    with pytest.raises(DbUnavailable) as excinfo:
        with connection():
            pass
    assert "sekritpw" not in str(excinfo.value)


# ── pool lifecycle ───────────────────────────────────────────────────────────────────────────


def test_pool_is_a_singleton_until_closed(monkeypatch):
    _configure_unreachable(monkeypatch)
    first = get_pool()
    assert first is not None
    assert get_pool() is first
    close_pool()
    second = get_pool()
    assert second is not None
    assert second is not first


def test_close_pool_is_idempotent():
    close_pool()
    close_pool()  # closing a never-opened / already-closed pool must be a no-op
    assert app_db.pool._pool is None
