"""CSRF guard (issue #11): same-origin + content-type enforcement on state-changing routes.

The guard is an app-wide dependency (``app.main.enforce_same_origin``); these tests exercise it
end-to-end through the ASGI stack so header handling, ordering, and the 403 envelope are the real
thing. ``POST /api/refresh`` is the probe route because it is the one whose lack of body model made
it CSRF-reachable in the first place.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from app.db import reset_db_settings
from app.main import create_app
from app.routers import refresh as refresh_mod

JSON = {"Content-Type": "application/json"}


class _RefreshSettings:
    """Minimal stand-in for the endpoint's settings, rooted at a tmp dir (mirrors test_refresh)."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.refresh_cooldown_seconds = 20
        self.agentic_account_masked = "••••4025"

    @property
    def refresh_request_path(self):
        return self.data_dir / "refresh.request"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # POSTURE — pre-auth stand-down, on purpose: AUTH_DATABASE_URL is explicitly absent, so
    # enforce_authenticated stands down and every status code below is the CSRF guard's own
    # verdict, not a 401 from the session layer. Auth enforcement composing OVER this guard is
    # pinned separately in test_auth_routes.py. (conftest already strips the DSNs and disables
    # backend/.env; the delenv here makes the posture this suite asserts visible in this file.)
    monkeypatch.delenv("AUTH_DATABASE_URL", raising=False)
    reset_db_settings()
    # Point the refresh endpoint at tmp_path so an *allowed* request can really queue, and reset
    # the module-global cooldown clock so tests don't bleed into each other.
    monkeypatch.setattr(refresh_mod, "get_settings", lambda: _RefreshSettings(tmp_path))
    monkeypatch.setattr(refresh_mod, "_last_request_monotonic", None)
    return TestClient(create_app())


# --- blocked: the cross-site shapes ----------------------------------------------------------


def test_cross_site_form_post_rejected(client):
    # The attack from the finding: an auto-submitting form is limited to form content types.
    res = client.post("/api/refresh", data={"go": "1"}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


def test_missing_content_type_rejected(client):
    # A bodyless cross-site POST (fetch with no body) carries no content type — still not JSON.
    res = client.post("/api/refresh")
    assert res.status_code == 403


def test_cross_origin_json_rejected(client):
    res = client.post("/api/refresh", json={}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


def test_sec_fetch_site_cross_site_rejected_even_with_allowed_origin(client):
    # Sec-Fetch-Site is checked first: the browser's own attestation outranks the Origin value.
    res = client.post(
        "/api/refresh",
        json={},
        headers={"Origin": "http://localhost:3100", "Sec-Fetch-Site": "cross-site"},
    )
    assert res.status_code == 403


def test_null_origin_rejected(client):
    # Sandboxed iframes and file:// pages send the literal serialization "null".
    res = client.post("/api/refresh", json={}, headers={"Origin": "null"})
    assert res.status_code == 403


def test_origin_prefix_bypass_rejected(client):
    # Must be a fullmatch against the localhost regex — a prefix match would let this through.
    res = client.post("/api/refresh", json={}, headers={"Origin": "http://localhost:3100.evil.example"})
    assert res.status_code == 403


# --- allowed: every legitimate caller shape ---------------------------------------------------


def test_same_origin_browser_allowed(client):
    # Production shape: single origin behind Caddy, browser attests via Sec-Fetch-Site.
    res = client.post("/api/refresh", json={}, headers={"Sec-Fetch-Site": "same-origin"})
    assert res.status_code == 200
    assert res.json()["status"] == "queued"


def test_allowlisted_origin_allowed(client):
    # Local dev shape: frontend on a random localhost port, matched by the CORS regex.
    res = client.post("/api/refresh", json={}, headers={"Origin": "http://127.0.0.1:43210"})
    assert res.status_code == 200
    assert res.json()["status"] == "queued"


def test_non_browser_json_allowed(client):
    # curl/scripts send neither Origin nor Sec-Fetch-Site; CSRF needs a victim browser to attach
    # ambient credentials, so these stay usable (with the JSON content type).
    res = client.post("/api/refresh", json={}, headers=JSON)
    assert res.status_code == 200
    assert res.json()["status"] == "queued"


def test_safe_methods_not_guarded(client):
    # Reads stay open to any origin — CORS governs response visibility for those.
    res = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert res.status_code == 200


def test_block_is_logged_loudly(client, caplog):
    # Standing rule: a guardrail never blocks silently — the block, route, and reason are logged.
    with caplog.at_level(logging.WARNING, logger="agentic.api"):
        client.post("/api/refresh", data={"go": "1"}, headers={"Origin": "https://evil.example"})
    assert "CSRF guard BLOCKED" in caplog.text
    assert "/api/refresh" in caplog.text
    assert "evil.example" in caplog.text
