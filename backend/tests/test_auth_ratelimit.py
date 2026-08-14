"""Route-wide auth cooldown (AUTH_THREAT_MODEL §5.1/§3.3) + the WindowLimiter it rides on.

This gate is the compensating control for §5.8's deliberate design choice that the PASSWORD step
has no per-account counter (the lockout counter lives on operator_totp and only the TOTP step
bumps it), and the cap on the login handler's 64 MiB-per-request Argon2 memory burn.

No database on purpose: the gate sits in a router dependency and must refuse BEFORE any
credential work, so an unreachable auth store is exactly the right harness — a blocked request
never finds out the store was down, and an admitted one gets the fail-closed 503.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import app.ratelimit as ratelimit_mod
from app.db import close_pool, reset_db_settings
from app.main import create_app
from app.ratelimit import WindowLimiter
from app.routers import auth as auth_routes

JSON = {"Content-Type": "application/json"}
# Refused instantly (connection refused on the discard port) — nothing listens there.
UNREACHABLE_AUTH_DSN = "postgresql://rh_auth:pw@127.0.0.1:9/nope"

OP1 = {"email": "op1@example.com", "password": "pw"}
OP2 = {"email": "op2@example.com", "password": "pw"}


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("AUTH_DATABASE_URL", UNREACHABLE_AUTH_DSN)
    reset_db_settings()
    close_pool()
    yield
    close_pool()
    reset_db_settings()


@pytest.fixture()
def budget_of_three(monkeypatch):
    """Shrink the route budget so tests trip it without twelve Argon2-priced requests."""
    monkeypatch.setattr(auth_routes, "AUTH_RATE_MAX_REQUESTS", 3)
    monkeypatch.setattr(auth_routes, "AUTH_RATE_WINDOW_SECONDS", 60.0)


def _client() -> TestClient:
    return TestClient(create_app(), base_url="https://testserver")


# --- WindowLimiter unit behavior -------------------------------------------------------------
# (CooldownLimiter's own tests live in test_ratelimit.py; the window variant is pinned here,
# next to the only endpoint family that uses it.)


def test_window_limiter_admits_up_to_budget_then_blocks():
    lim = WindowLimiter()
    assert [lim.check_and_consume(3, 60) for _ in range(3)] == [0, 0, 0]
    wait = lim.check_and_consume(3, 60)
    assert 1 <= wait <= 61


def test_window_limiter_slots_free_as_grants_age_out(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(ratelimit_mod.time, "monotonic", lambda: clock["now"])
    lim = WindowLimiter()
    assert lim.check_and_consume(2, 60) == 0  # t=1000
    clock["now"] += 30.0
    assert lim.check_and_consume(2, 60) == 0  # t=1030, budget full
    assert lim.check_and_consume(2, 60) > 0
    clock["now"] += 31.0  # t=1061: the t=1000 grant has aged out, the t=1030 one has not
    assert lim.check_and_consume(2, 60) == 0
    assert lim.check_and_consume(2, 60) > 0


def test_window_limiter_blocked_calls_do_not_extend_the_wait(monkeypatch):
    """Only ADMITTED requests consume budget — hammering while blocked must not push the
    caller's own recovery further out (mirrors CooldownLimiter's refused-call semantics)."""
    clock = {"now": 0.0}
    monkeypatch.setattr(ratelimit_mod.time, "monotonic", lambda: clock["now"])
    lim = WindowLimiter()
    assert lim.check_and_consume(1, 60) == 0
    for _ in range(50):  # a rejection storm
        assert lim.check_and_consume(1, 60) == 61  # full window left, +1 rounding
    clock["now"] = 61.0  # the single grant ages out exactly as if the storm never happened
    assert lim.check_and_consume(1, 60) == 0


def test_window_limiter_nonpositive_config_disables():
    assert WindowLimiter().check_and_consume(0, 60) == 0
    assert WindowLimiter().check_and_consume(3, 0) == 0


def test_window_limiter_reset_clears_the_gate():
    lim = WindowLimiter()
    assert lim.check_and_consume(1, 60) == 0
    assert lim.check_and_consume(1, 60) > 0
    lim.reset()
    assert lim.check_and_consume(1, 60) == 0


# --- The route gate --------------------------------------------------------------------------


def test_login_limiter_is_per_route_not_per_account(configured, budget_of_three, caplog):
    """§5.1 (spec-named test): N failed logins ALTERNATING between two operators still trip the
    limiter at N — the budget belongs to the route, not the account, so password spraying across
    both operators buys nothing. Also pins the two §7.2 observability halves: a loud server log
    naming the reason, and the 423 + retry_after shape the built frontend renders honestly."""
    client = _client()
    with caplog.at_level(logging.WARNING, logger="agentic.auth.routes"):
        for i in range(3):
            body = OP1 if i % 2 == 0 else OP2
            res = client.post("/api/auth/login", json=body, headers=JSON)
            assert res.status_code == 503  # admitted by the gate; refused by the down store
        blocked = client.post("/api/auth/login", json=OP1, headers=JSON)
    assert blocked.status_code == 423
    detail = blocked.json()["detail"]
    # frontend/src/lib/auth.ts::extractRetryAfter reads detail.retry_after and maps 423 to an
    # honest "locked, retry in N s" state — this shape is the wire contract, do not change it.
    assert detail["status"] == "locked"
    assert isinstance(detail["retry_after"], int) and detail["retry_after"] >= 1
    assert detail["reason"]
    # Never a silent block: the gate names itself and the reason in the server log.
    assert "auth rate limit hit" in caplog.text
    assert "POST /api/auth/login" in caplog.text


def test_budget_is_shared_across_every_auth_post_route(configured, budget_of_three):
    """One budget over all of /api/auth/*: requests spread across login/verify/resend drain the
    same gate the TOTP route draws from, so no auth POST has weaker limiting than another."""
    client = _client()
    for path, payload in (
        ("/api/auth/login", OP1),
        ("/api/auth/verify", {"token": "t"}),
        ("/api/auth/verify/resend", {"email": OP1["email"]}),
    ):
        assert client.post(path, json=payload, headers=JSON).status_code != 423
    res = client.post(
        "/api/auth/login/totp", json={"challenge_token": "t", "code": "1"}, headers=JSON
    )
    assert res.status_code == 423


def test_blocked_request_never_reaches_credential_verification(configured, budget_of_three, monkeypatch):
    """The memory-DoS half of the finding: a blocked login must cost ZERO Argon2 work. The gate
    is a dependency, so it refuses before the handler — password_login (the 64 MiB verification)
    is never invoked for a 423."""
    client = _client()
    for _ in range(3):
        client.post("/api/auth/login", json=OP1, headers=JSON)
    calls: list[int] = []
    monkeypatch.setattr(
        auth_routes.auth_service, "password_login", lambda *a, **k: calls.append(1)
    )
    blocked = client.post("/api/auth/login", json=OP1, headers=JSON)
    assert blocked.status_code == 423
    assert calls == []


def test_get_me_is_exempt_and_cannot_starve_the_login_budget(configured, monkeypatch):
    """GET /api/auth/me is the frontend's only source of auth state, fired on every page load.
    It does no credential work, so it is deliberately outside the budget — metering it would let
    ordinary page traffic starve logins and silently render the operator logged out."""
    monkeypatch.setattr(auth_routes, "AUTH_RATE_MAX_REQUESTS", 1)
    client = _client()
    for _ in range(5):
        assert client.get("/api/auth/me").status_code == 401  # never 423, consumes nothing
    # The single POST slot is still there — the GETs drew nothing from it...
    assert client.post("/api/auth/login", json=OP1, headers=JSON).status_code == 503
    # ...and the gate itself still works.
    assert client.post("/api/auth/login", json=OP1, headers=JSON).status_code == 423


def test_each_app_instance_carries_its_own_gate(configured, budget_of_three):
    """The limiter lives on app.state, not as a module global: one app per process in production
    (so still the single §3.3 in-process budget), but a fresh gate per created app, so parallel
    test apps — and the rest of this suite — never share exhaustion state."""
    a = _client()
    b = _client()
    for _ in range(3):
        a.post("/api/auth/login", json=OP1, headers=JSON)
    assert a.post("/api/auth/login", json=OP1, headers=JSON).status_code == 423
    assert b.post("/api/auth/login", json=OP1, headers=JSON).status_code == 503  # untouched


def test_default_budget_admits_a_full_legitimate_login_flow(configured):
    """The guardrail rule: a limiter must never block valid use. At the DEFAULT budget, a
    realistic worst-case honest session — two fat-fingered password attempts, a full
    password+TOTP login, a logout, and a re-login — stays entirely inside the gate."""
    client = _client()
    flow = [
        ("/api/auth/login", OP1),
        ("/api/auth/login", OP1),
        ("/api/auth/login", OP1),
        ("/api/auth/login/totp", {"challenge_token": "t", "code": "123456"}),
        ("/api/auth/logout", {}),
        ("/api/auth/login", OP1),
        ("/api/auth/login/totp", {"challenge_token": "t", "code": "123456"}),
    ]
    for path, payload in flow:
        res = client.post(path, json=payload, headers=JSON)
        assert res.status_code != 423, f"{path} was rate-limited inside a legitimate flow"


# --- Per-client keying (§5.13) ----------------------------------------------------------------
# Added after the outer Caddy basic-auth gate was removed. While that gate stood, a single unkeyed
# budget was survivable: strangers could not reach the login form at all. Without it, one shared
# budget means ANY caller can spend it and deny sign-in to the operators — a denial-of-service on
# the account owners, delivered by a stranger sending a dozen requests a minute.


def test_one_client_over_budget_does_not_block_another(configured, budget_of_three):
    """THE regression this split exists to prevent: an attacker must be able to lock out only
    themselves. Before per-client keying every one of these assertions passed for the wrong
    reason — the second client was refused because the FIRST had spent the shared budget."""
    c = _client()
    attacker = {**JSON, "CF-Connecting-IP": "203.0.113.7"}
    operator = {**JSON, "CF-Connecting-IP": "198.51.100.22"}

    for _ in range(3):
        c.post("/api/auth/login", json=OP1, headers=attacker)
    blocked = c.post("/api/auth/login", json=OP1, headers=attacker)
    assert blocked.status_code == 423, "the attacker must hit their own ceiling"

    # The operator's request must still be admitted — it reaches the (unreachable) auth DB and
    # fails closed with 503, which proves it got PAST the gate rather than being refused by it.
    theirs = c.post("/api/auth/login", json=OP2, headers=operator)
    assert theirs.status_code == 503, (
        f"a different client must not be rate-limited by the attacker's spending, got "
        f"{theirs.status_code}"
    )


def test_global_ceiling_still_caps_total_work_across_many_clients(configured, monkeypatch):
    """The per-client gate decides WHO is refused; the global one bounds TOTAL Argon2 work. A
    caller rotating the header must still meet a ceiling, or per-client keying would have traded
    the denial-of-service for an unbounded-CPU hole."""
    monkeypatch.setattr(auth_routes, "AUTH_RATE_MAX_REQUESTS", 3)
    monkeypatch.setattr(auth_routes, "AUTH_RATE_GLOBAL_MAX_REQUESTS", 5)
    monkeypatch.setattr(auth_routes, "AUTH_RATE_WINDOW_SECONDS", 60.0)
    c = _client()
    seen = [
        c.post(
            "/api/auth/login", json=OP1, headers={**JSON, "CF-Connecting-IP": f"203.0.113.{i}"}
        ).status_code
        for i in range(8)
    ]
    assert seen.count(423) >= 1, "a header-rotating caller must still hit the global ceiling"
    assert 503 in seen, "early requests must have been admitted (503 = past the gate)"


def test_forged_or_absent_header_never_escapes_the_gate(configured, budget_of_three):
    """A malformed header must not become a key, and a caller sending none must not be exempt:
    both fall back to a shared bucket rather than an unmetered path."""
    c = _client()
    junk = {**JSON, "CF-Connecting-IP": "not-an-ip-address; drop table"}
    codes = [c.post("/api/auth/login", json=OP1, headers=junk).status_code for _ in range(5)]
    assert 423 in codes, "garbage in the header must not buy an unmetered request"


def test_rate_limit_key_prefers_edge_header_then_peer():
    """Unit-level: the key is the normalised edge header when present and valid, the peer address
    otherwise. Normalisation matters — two spellings of one address must not be two budgets."""

    class _Req:
        def __init__(self, headers, peer):
            self.headers = headers
            self.client = type("C", (), {"host": peer})() if peer else None

    assert auth_routes.rate_limit_key(_Req({"cf-connecting-ip": "203.0.113.9"}, "10.0.0.1")) == "203.0.113.9"
    # IPv6 written two ways is ONE client, so it must normalise to one key.
    a = auth_routes.rate_limit_key(_Req({"cf-connecting-ip": "2001:0db8:0000::1"}, None))
    b = auth_routes.rate_limit_key(_Req({"cf-connecting-ip": "2001:db8::1"}, None))
    assert a == b, "two spellings of one address must share one budget"
    assert auth_routes.rate_limit_key(_Req({}, "10.0.0.5")) == "10.0.0.5"
    assert auth_routes.rate_limit_key(_Req({"cf-connecting-ip": "garbage"}, "10.0.0.5")) == "10.0.0.5"
    assert auth_routes.rate_limit_key(_Req({}, None)) == "unidentified"


def test_keyed_limiter_evicts_oldest_keys_and_stays_bounded():
    """The per-key dict is fed attacker-chosen keys, so it is itself a memory-exhaustion vector
    unless bounded — the exact bug class this gate exists to prevent."""
    lim = ratelimit_mod.KeyedWindowLimiter(max_keys=4)
    for i in range(50):
        assert lim.check_and_consume(f"key-{i}", 3, 60) == 0
    assert len(lim._buckets) <= 4


def test_blocked_client_cannot_evict_itself_into_a_fresh_budget():
    """A refusal must be sticky for the window. If a blocked key were dropped from the dict, the
    next request would look like a first-time caller and the gate would admit it forever."""
    lim = ratelimit_mod.KeyedWindowLimiter(max_keys=8)
    for _ in range(2):
        assert lim.check_and_consume("victim", 2, 60) == 0
    assert lim.check_and_consume("victim", 2, 60) > 0
    for _ in range(20):  # churn other keys past max_keys
        lim.check_and_consume("noise", 2, 60)
    assert lim.check_and_consume("victim", 2, 60) > 0, "the refusal must survive key churn"
