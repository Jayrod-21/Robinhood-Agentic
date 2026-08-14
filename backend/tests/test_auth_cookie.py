"""§5.3 cookie pinning: name, attributes, and where a session cookie may (and may not) appear.

The cookie NAME is load-bearing (AUTH_THREAT_MODEL §5.3): ww.jaredstudio.com shares its
registrable domain with korean./uvrl./uvrl-study. siblings, and only the __Host- prefix stops a
compromised sibling from planting a Domain=jaredstudio.com cookie here (SameSite does not help —
siblings are same-site). These tests fail on: a renamed cookie, a dropped HttpOnly/Secure/
SameSite/Path attribute, an added Domain attribute, or a Secure flag derived from the request
scheme instead of the constant.

No database needed: the service layer is stubbed so the ROUTE's cookie emission is what is under
test, byte for byte.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import auth as auth_service

JSON = {"Content-Type": "application/json"}
FAKE_SESSION_TOKEN = "t" * 43  # shaped like a real 32-byte urlsafe token
FAKE_CHALLENGE = {"challenge_token": "c" * 43, "code": "123456"}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(auth_service, "complete_mfa", lambda *a, **k: FAKE_SESSION_TOKEN)
    monkeypatch.setattr(
        auth_service,
        "password_login",
        lambda *a, **k: auth_service.IssuedChallenge(token="c" * 43, expires_in=300),
    )
    monkeypatch.setattr(auth_service, "revoke_session", lambda *a, **k: None)
    return TestClient(create_app(), base_url="https://testserver")


def _session_cookie_header(res) -> str:
    header = res.headers.get("set-cookie")
    assert header is not None, "expected a Set-Cookie header"
    return header


def test_cookie_name_has_host_prefix_and_no_domain_attribute(client):
    res = client.post("/api/auth/login/totp", json=FAKE_CHALLENGE, headers=JSON)
    assert res.status_code == 204
    header = _session_cookie_header(res)
    # The name, exactly — "rename the cookie" must fail a test, not look harmless (§5.3).
    assert header.startswith("__Host-rh_sid="), header
    assert auth_service.SESSION_COOKIE_NAME == "__Host-rh_sid"
    # __Host- REQUIRES: no Domain, Secure, Path=/. The browser enforces it; we pin it.
    assert "domain" not in header.lower(), header
    assert "path=/" in header.lower(), header


def test_cookie_carries_httponly_secure_samesite_strict(client):
    header = _session_cookie_header(
        client.post("/api/auth/login/totp", json=FAKE_CHALLENGE, headers=JSON)
    )
    low = header.lower()
    assert "httponly" in low, header  # §5.10: not readable from JS
    assert "secure" in low, header  # §5.3: never on a plaintext wire
    assert "samesite=strict" in low, header  # §5.9: never attached cross-site


def test_secure_flag_is_constant_not_derived_from_forwarded_proto(client):
    """§5.3: Secure comes from a constant. X-Forwarded-Proto: http must not strip it."""
    res = client.post(
        "/api/auth/login/totp",
        json=FAKE_CHALLENGE,
        headers={**JSON, "X-Forwarded-Proto": "http"},
    )
    assert res.status_code == 204
    assert "secure" in _session_cookie_header(res).lower()


def test_password_step_never_sets_a_cookie(client):
    """§5.1/§4: the password step mints a challenge, NEVER a session — no Set-Cookie at all."""
    res = client.post(
        "/api/auth/login", json={"email": "op@example.com", "password": "pw"}, headers=JSON
    )
    assert res.status_code == 200
    assert res.json()["status"] == "mfa_required"
    assert "set-cookie" not in res.headers


def test_logout_clears_the_same_host_prefixed_cookie(client):
    res = client.post("/api/auth/logout", json={}, headers=JSON)
    assert res.status_code == 204
    header = _session_cookie_header(res)
    assert header.startswith("__Host-rh_sid=")
    assert "domain" not in header.lower()
    assert "path=/" in header.lower()
