"""CSRF guard (issue #11): same-origin + content-type enforcement on state-changing routes.

The guard is an app-wide dependency (``app.main.enforce_same_origin``); these tests exercise it
end-to-end through the ASGI stack so header handling, ordering, and the 403 envelope are the real
thing. ``POST /api/history/thesis`` is the probe route. It used to be ``POST /api/refresh`` — the route
whose lack of a body model made it CSRF-reachable in the first place — but that endpoint is gone
along with the Robinhood refresh bridge it drove.

WHAT THE ALLOWED CASES ASSERT NOW, AND WHY IT CHANGED
    The old probe returned 200 with ``{"status": "queued"}``, so an allowed request had a cheerful
    success to check. No remaining state-changing route can reach 200 in this posture — they all
    need a database, and this suite deliberately runs with the DSNs stripped so every code below is
    the CSRF guard's own verdict.

    So the allowed cases assert what is actually being tested: the guard did NOT reject, and logged
    no block. The request reaching a 503 from the database layer is proof it got PAST the guard,
    which is the whole claim. The blocked cases are unchanged and still assert an exact 403.
"""

import logging

import pytest
from app.db import reset_db_settings
from app.main import create_app
from fastapi.testclient import TestClient

JSON = {"Content-Type": "application/json"}

# A state-changing POST that exists and is covered by the app-wide guard.
PROBE = "/api/history/thesis"


def _assert_passed_the_guard(res) -> None:
    """The request reached the handler rather than being rejected by the CSRF guard.

    Asserting a specific downstream code would pin this suite to the journal endpoint's behaviour,
    which is not what it tests. 403 is the guard's only verdict, so its ABSENCE is the assertion.
    """
    assert res.status_code != 403, f"the guard rejected a legitimate caller: {res.text[:200]}"


@pytest.fixture()
def client(monkeypatch):
    # POSTURE — pre-auth stand-down, on purpose: AUTH_DATABASE_URL is explicitly absent, so
    # enforce_authenticated stands down and every status code below is the CSRF guard's own
    # verdict, not a 401 from the session layer. Auth enforcement composing OVER this guard is
    # pinned separately in test_auth_routes.py. (conftest already strips the DSNs and disables
    # backend/.env; the delenv here makes the posture this suite asserts visible in this file.)
    monkeypatch.delenv("AUTH_DATABASE_URL", raising=False)
    reset_db_settings()
    return TestClient(create_app())


# --- blocked: the cross-site shapes ----------------------------------------------------------


def test_cross_site_form_post_rejected(client):
    # The attack from the finding: an auto-submitting form is limited to form content types.
    res = client.post(PROBE, data={"go": "1"}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


def test_missing_content_type_rejected(client):
    # A bodyless cross-site POST (fetch with no body) carries no content type — still not JSON.
    res = client.post(PROBE)
    assert res.status_code == 403


def test_cross_origin_json_rejected(client):
    res = client.post(PROBE, json={}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


def test_sec_fetch_site_cross_site_rejected_even_with_allowed_origin(client):
    # Sec-Fetch-Site is checked first: the browser's own attestation outranks the Origin value.
    res = client.post(
        PROBE,
        json={},
        headers={"Origin": "http://localhost:3100", "Sec-Fetch-Site": "cross-site"},
    )
    assert res.status_code == 403


def test_null_origin_rejected(client):
    # Sandboxed iframes and file:// pages send the literal serialization "null".
    res = client.post(PROBE, json={}, headers={"Origin": "null"})
    assert res.status_code == 403


def test_origin_prefix_bypass_rejected(client):
    # Must be a fullmatch against the localhost regex — a prefix match would let this through.
    res = client.post(PROBE, json={}, headers={"Origin": "http://localhost:3100.evil.example"})
    assert res.status_code == 403


# --- allowed: every legitimate caller shape ---------------------------------------------------


def test_same_origin_browser_allowed(client):
    # Production shape: single origin behind Caddy, browser attests via Sec-Fetch-Site.
    res = client.post(PROBE, json={}, headers={"Sec-Fetch-Site": "same-origin"})
    _assert_passed_the_guard(res)


def test_allowlisted_origin_allowed(client):
    # Local dev shape: frontend on a random localhost port, matched by the CORS regex.
    res = client.post(PROBE, json={}, headers={"Origin": "http://127.0.0.1:43210"})
    _assert_passed_the_guard(res)


def test_non_browser_json_allowed(client):
    # curl/scripts send neither Origin nor Sec-Fetch-Site; CSRF needs a victim browser to attach
    # ambient credentials, so these stay usable (with the JSON content type).
    res = client.post(PROBE, json={}, headers=JSON)
    _assert_passed_the_guard(res)


def test_safe_methods_not_guarded(client):
    # Reads stay open to any origin — CORS governs response visibility for those.
    res = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert res.status_code == 200


def test_block_is_logged_loudly(client, caplog):
    # Standing rule: a guardrail never blocks silently — the block, route, and reason are logged.
    with caplog.at_level(logging.WARNING, logger="agentic.api"):
        client.post(PROBE, data={"go": "1"}, headers={"Origin": "https://evil.example"})
    assert "CSRF guard BLOCKED" in caplog.text
    assert PROBE in caplog.text
    assert "evil.example" in caplog.text
