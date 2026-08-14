"""/api/db/health + /api/history/* degradation contract, end to end through the ASGI stack.

The property that matters most (issue #6): the dashboard serves TODAY with no database, and it
must keep doing so. So with no DSN — and with an unreachable DSN — the app still builds, the
existing health endpoint still answers 200, DB health answers 200 with an honest report, and
every history route answers a clear 503 instead of hanging or 500ing.
"""

from __future__ import annotations

import pytest
from app.db import close_pool, reset_db_settings
from app.main import create_app
from fastapi.testclient import TestClient

JSON = {"Content-Type": "application/json"}

UNREACHABLE_DSN = "postgresql://rh_app:sekritpw@127.0.0.1:9/rh"

VALID_EXIT_BODY = {
    "portfolio_id": 1,
    "symbol": "TSM",
    "entry_date": "2026-06-03",
    "exit_date": "2026-08-13",
    "exit_price": "455.00",
    "exit_reason": "thesis played out; exit into catalyst gap",
}


@pytest.fixture(autouse=True)
def clean_db_state(monkeypatch):
    # POSTURE — pre-auth stand-down, on purpose: AUTH_DATABASE_URL is explicitly absent, so the
    # history routes answer with their DEGRADATION status codes (503/422/403), not the session
    # layer's 401. The authenticated posture for these same routes is pinned in
    # test_auth_routes.py. (conftest strips the DSNs and disables backend/.env; the delenvs here
    # make the asserted posture visible in this file.)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTH_DATABASE_URL", raising=False)
    reset_db_settings()
    close_pool()
    yield
    close_pool()
    reset_db_settings()


@pytest.fixture()
def client():
    return TestClient(create_app())


@pytest.fixture()
def client_unreachable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DSN)
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("DB_ACQUIRE_TIMEOUT_SECONDS", "0.5")
    reset_db_settings()
    return TestClient(create_app())


# ── no database configured ───────────────────────────────────────────────────────────────────


def test_app_health_still_serves_without_db(client):
    """THE graceful-degradation pin: the dashboard's own health is untouched by a missing DB."""
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_db_health_answers_200_not_configured(client):
    res = client.get("/api/db/health")
    assert res.status_code == 200
    body = res.json()
    assert body["configured"] is False
    assert body["reachable"] is False
    assert body["error"] == "DATABASE_URL is not set"


def test_history_reads_degrade_to_503(client):
    res = client.get("/api/history/entries")
    assert res.status_code == 503
    assert "no database configured" in res.json()["detail"]


def test_history_writes_degrade_to_503(client):
    res = client.post("/api/history/exits", json=VALID_EXIT_BODY, headers=JSON)
    assert res.status_code == 503
    assert "no database configured" in res.json()["detail"]

    res = client.post(
        "/api/history/lessons",
        json={"title": "t", "lesson": "never average down"},
        headers=JSON,
    )
    assert res.status_code == 503


# ── database configured but unreachable (backend is not on rh-internal — ADR-001) ────────────


def test_db_health_reports_unreachable_without_hanging(client_unreachable):
    res = client_unreachable.get("/api/db/health")
    assert res.status_code == 200
    body = res.json()
    assert body["configured"] is True
    assert body["reachable"] is False
    assert body["error"]
    # The DSN (with the rh_app password) must never surface in an API response.
    assert "sekritpw" not in res.text


def test_history_routes_degrade_when_db_unreachable(client_unreachable):
    res = client_unreachable.get("/api/history/entries")
    assert res.status_code == 503
    assert "sekritpw" not in res.text

    res = client_unreachable.post("/api/history/exits", json=VALID_EXIT_BODY, headers=JSON)
    assert res.status_code == 503


def test_rest_of_dashboard_unaffected_by_unreachable_db(client_unreachable):
    res = client_unreachable.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


# ── input validation happens before any DB touch ─────────────────────────────────────────────


def test_bad_symbol_is_422_not_db_error(client):
    res = client.get("/api/history/entries", params={"symbol": "not a symbol!!"})
    assert res.status_code == 422


def test_exit_body_validation(client):
    bad = dict(VALID_EXIT_BODY, exit_price="-1")
    res = client.post("/api/history/exits", json=bad, headers=JSON)
    assert res.status_code == 422

    bad = dict(VALID_EXIT_BODY, exit_reason="")
    res = client.post("/api/history/exits", json=bad, headers=JSON)
    assert res.status_code == 422


def test_history_posts_covered_by_csrf_guard(client):
    """The app-wide same-origin guard must apply to the new state-changing routes too."""
    res = client.post(
        "/api/history/lessons",
        json={"title": "t", "lesson": "x"},
        headers={**JSON, "Origin": "https://evil.example"},
    )
    assert res.status_code == 403
