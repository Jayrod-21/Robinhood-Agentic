"""Route surface + app-wide enforcement wiring (AUTH_THREAT_MODEL §4, §5.2, §5.7, §5.9).

Covers, without a database:
  * the app-wide ``enforce_authenticated`` dependency: allow-list is /api/health + /api/auth/*
    and NOTHING else, proven by enumerating EVERY reachable route (recursively — see
    ``_walk_routes``) rather than sampling; every other route answers 401 without a session once
    AUTH_DATABASE_URL is configured — including routers added later, because the dependency is
    registered on the app.
  * §4 closure of the bootstrap surface: /openapi.json, /docs, /redoc are Starlette Routes that
    dodge ``FastAPI(dependencies=...)`` entirely, so they are disabled outright in main.py; 404
    is pinned here.
  * the pre-deployment posture: with AUTH_DATABASE_URL unset the dependency stands down and the
    dashboard serves as it always has (behind the Caddy outer gate, §5.13).
  * §5.7: there is NO password-reset/forgot route, pinned so adding one is a conscious act that
    fails a test rather than a quiet convenience.
  * §5.9 composition: the auth routes sit behind the same CSRF guard as everything else.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.db import close_pool, reset_db_settings
from app.main import create_app

JSON = {"Content-Type": "application/json"}
# Refused instantly (connection refused on the discard port) — nothing listens there.
UNREACHABLE_AUTH_DSN = "postgresql://rh_auth:pw@127.0.0.1:9/nope"


def _walk_routes(routes):
    """Yield every route-like object reachable from ``routes``, unwrapping router wrappers.

    Under this FastAPI build, ``include_router`` leaves ``_IncludedRouter`` wrapper objects on
    ``app.routes`` that carry NO ``.path`` of their own — the real routes are only reachable
    through ``effective_route_contexts()``, whose contexts carry the effective (fully prefixed)
    ``.path`` and ``.methods``. A flat ``[getattr(r, "path", "") for r in app.routes]`` scan
    therefore sees only the bootstrap routes plus empty strings, which is how the original
    version of the §5.7 test stayed green with a live ``POST /api/auth/reset`` added to the app.
    Unwrap every shape, and let callers pin non-emptiness so a future framework change fails
    loudly instead of hollowing the enumeration out again."""
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            yield from contexts()
            continue
        yield route
        yield from _walk_routes(getattr(route, "routes", []))


def _route_surface(app) -> set[tuple[str, str]]:
    """Every (method, path) pair the app can actually serve."""
    surface: set[tuple[str, str]] = set()
    for route in _walk_routes(app.routes):
        path = getattr(route, "path", None)
        if path is None:
            continue
        for method in getattr(route, "methods", None) or set():
            surface.add((method, path))
    return surface


@pytest.fixture()
def unconfigured(monkeypatch):
    monkeypatch.delenv("AUTH_DATABASE_URL", raising=False)
    reset_db_settings()
    close_pool()
    yield
    close_pool()
    reset_db_settings()


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("AUTH_DATABASE_URL", UNREACHABLE_AUTH_DSN)
    reset_db_settings()
    close_pool()
    yield
    close_pool()
    reset_db_settings()


def test_no_password_reset_route_is_exposed():
    """§5.7: the largest MFA-bypass class is removed by the route not existing. Recovery is the
    host CLI. If someone adds /reset or /forgot-password, this test makes it a deliberate act."""
    app = create_app()
    paths = {path for _, path in _route_surface(app)}
    # Anti-hollow guard: the enumeration must be seeing the REAL route surface. The flat scan
    # this replaces returned only bootstrap paths and empty strings, so its offender check
    # could never fail (proven against a live /api/auth/reset). If the walk ever goes blind
    # again, fail here — loudly — rather than pass forever.
    assert "/api/auth/login" in paths and "/api/account" in paths, (
        f"route enumeration is not seeing real routes; saw only: {sorted(paths)}"
    )
    offenders = [p for p in sorted(paths) if re.search(r"reset|forgot|recover", p, re.IGNORECASE)]
    assert offenders == [], f"password-reset-shaped routes must not exist: {offenders}"


def test_unconfigured_auth_stands_down_and_dashboard_serves(unconfigured):
    """Pre-deployment posture: no AUTH_DATABASE_URL means no operators can exist; the dashboard
    keeps serving behind the Caddy outer gate exactly as before auth shipped (§5.13)."""
    client = TestClient(create_app())
    assert client.get("/api/db/health").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_configured_auth_gates_every_non_allowlisted_route(configured):
    """Once configured, a request without a session cookie is 401 on every router — no DB round
    trip is needed to refuse an absent credential, so this holds even with the DB unreachable."""
    client = TestClient(create_app(), base_url="https://testserver")
    # A sample from three different routers, none of which mention auth anywhere in their code —
    # coverage comes from the app-wide registration, not from per-router wiring (§4).
    assert client.get("/api/db/health").status_code == 401
    assert client.get("/api/account").status_code == 401
    assert client.get("/api/history/entries").status_code == 401


def test_allow_list_is_health_and_auth_routes_only(configured):
    app = create_app()

    # A lookalike prefix must NOT be allow-listed: register a real /api/authz route and prove the
    # app-wide dependency gates it. (Routes added after create_app still inherit the app-level
    # dependencies — FastAPI folds them in at registration.) This replaces a tautology that
    # asserted a Python string fact and would have passed with enforce_authenticated deleted;
    # this version actually exercises the dependency's "/api/auth/" segment-boundary match.
    @app.get("/api/authz")
    def authz_probe():  # pragma: no cover — reaching this handler IS the failure
        raise AssertionError("/api/authz reached its handler without a session")

    client = TestClient(app, base_url="https://testserver")
    # /api/health is open.
    assert client.get("/api/health").status_code == 200
    # The auth routes are reachable (not 401 from the gate). With the store unreachable they
    # REFUSE with 503 — the fail-closed posture, never a default-allow (§8/§11.6).
    res = client.post(
        "/api/auth/login", json={"email": "op@example.com", "password": "pw"}, headers=JSON
    )
    assert res.status_code == 503
    assert client.get("/api/authz").status_code == 401


def test_allow_list_is_closed_over_every_reachable_route(configured):
    """§4 says /api/health, the auth routes, "and nothing else" — enumerated EXHAUSTIVELY, not
    sampled. A sample cannot catch a route that dodges the app-wide dependency the way
    /openapi.json did: FastAPI.setup() registers it (plus /docs, /redoc, /docs/oauth2-redirect)
    as plain Starlette Routes, not APIRoutes, so ``FastAPI(dependencies=[...])`` never applied and
    the full API schema — /api/refresh included — was served unauthenticated. Those routes are
    now disabled outright in main.py (pinned below); every route that remains must either be on
    the allow-list or answer 401 to a cookie-less request."""
    app = create_app()
    client = TestClient(app, base_url="https://testserver")
    surface = sorted(_route_surface(app))
    assert ("GET", "/api/account") in surface  # anti-hollow guard, see _walk_routes
    checked = 0
    for method, path in surface:
        if method in {"HEAD", "OPTIONS"}:
            continue  # auto-added companions of the GET/POST routes checked below
        concrete = re.sub(r"\{[^}]+\}", "probe", path)
        res = client.request(method, concrete, headers=JSON)
        checked += 1
        if path == "/api/health":
            assert res.status_code == 200
        elif path.startswith("/api/auth/"):
            # Allow-listed means "not refused by the session GATE" — /api/auth/me still answers
            # its own handler's 401 ("not authenticated") to a cookie-less request, which is
            # correct. The gate's refusal is distinguishable by its detail string.
            if res.status_code == 401:
                assert res.json().get("detail") != "authentication required", (
                    f"allow-listed {method} {path} was refused by the session gate"
                )
        else:
            assert res.status_code == 401, (
                f"{method} {path} answered {res.status_code} to a request with no session — "
                "the §4 allow-list is /api/health + /api/auth/* and NOTHING else"
            )
    assert checked >= 10, f"only {checked} routes enumerated — the walk went hollow"
    # The FastAPI bootstrap surface is deliberately ABSENT (main.py sets openapi_url=None,
    # docs_url=None, redoc_url=None), not quietly public: 404, never 200.
    for bootstrap in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"):
        assert client.get(bootstrap).status_code == 404, f"{bootstrap} must not be served"


def test_auth_routes_sit_behind_the_csrf_guard(unconfigured):
    """§5.9: one guard, one place — the app-wide enforce_same_origin covers the auth routes by
    construction. A cross-site form-shaped login POST dies at the guard, before any handler."""
    client = TestClient(create_app())
    res = client.post(
        "/api/auth/login",
        data={"email": "x@example.com", "password": "pw"},
        headers={"Origin": "https://evil.example"},
    )
    assert res.status_code == 403


def test_shapeless_cookie_is_refused_without_touching_the_db(configured):
    """A cookie value that cannot be a real token (wrong alphabet/length) is 401 immediately —
    the shape gate keeps garbage away from the credential store."""
    client = TestClient(create_app(), base_url="https://testserver")
    res = client.get("/api/db/health", headers={"Cookie": "__Host-rh_sid=short"})
    assert res.status_code == 401
