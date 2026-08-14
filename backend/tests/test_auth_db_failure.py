"""Fail-closed on DbUnavailable (AUTH_THREAT_MODEL §8, invariant §11.6).

THE invariant: an auth path that "degrades gracefully" when it cannot reach the credential store
is an authentication bypass. With AUTH_DATABASE_URL configured and the pool forced to raise
DbUnavailable, every auth-touching request must REFUSE with 503 — no session minted, no session
accepted, no 200 anywhere. Reverting the single translation point
(``app.services.auth._auth_db`` → :class:`AuthUnavailable`) turns these red.

The pool is forced to raise exactly the way ``pool.py::auth_connection`` does when the database
is down, so this covers the translation without needing a database to take down.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.db import DbUnavailable, close_pool, reset_db_settings
from app.main import create_app
from app.services import auth as auth_service

JSON = {"Content-Type": "application/json"}
SHAPED_TOKEN = "a" * 43  # passes the shape gate, so validation MUST consult the store
SHAPED_COOKIE = {"Cookie": f"__Host-rh_sid={SHAPED_TOKEN}"}


@contextmanager
def _pool_down():
    raise DbUnavailable("unreachable", "authentication is temporarily unavailable")
    yield  # pragma: no cover


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("AUTH_DATABASE_URL", "postgresql://rh_auth:pw@127.0.0.1:9/nope")
    reset_db_settings()
    close_pool()
    # services.auth imported auth_connection by name; patching it there is patching what the
    # single fail-closed boundary actually calls.
    monkeypatch.setattr(auth_service, "auth_connection", _pool_down)
    yield TestClient(create_app(), base_url="https://testserver")
    close_pool()
    reset_db_settings()


def test_protected_route_with_a_cookie_returns_503_never_200(client):
    """A presented session cannot be validated → it is NOT accepted. 503, not a pass-through."""
    res = client.get("/api/db/health", headers=SHAPED_COOKIE)
    assert res.status_code == 503
    res = client.get("/api/account", headers=SHAPED_COOKIE)
    assert res.status_code == 503


def test_login_refuses_and_mints_nothing(client):
    res = client.post(
        "/api/auth/login", json={"email": "op@example.com", "password": "pw"}, headers=JSON
    )
    assert res.status_code == 503
    assert "challenge_token" not in res.text
    assert "set-cookie" not in res.headers


def test_totp_step_refuses_and_sets_no_cookie(client):
    res = client.post(
        "/api/auth/login/totp",
        json={"challenge_token": SHAPED_TOKEN, "code": "123456"},
        headers=JSON,
    )
    assert res.status_code == 503
    assert "set-cookie" not in res.headers


def test_me_and_logout_refuse(client):
    """Logout must not pretend: with the store down, revocation cannot be confirmed, so the
    honest answer is 503 — never a 204 over a session that is still live server-side."""
    assert client.get("/api/auth/me", headers=SHAPED_COOKIE).status_code == 503
    res = client.post("/api/auth/logout", json={}, headers={**JSON, **SHAPED_COOKIE})
    assert res.status_code == 503


def test_error_detail_is_the_pool_message_never_a_dsn(client):
    res = client.get("/api/db/health", headers=SHAPED_COOKIE)
    detail = res.json()["detail"]
    assert detail == "authentication is temporarily unavailable"
    assert "postgresql://" not in res.text
