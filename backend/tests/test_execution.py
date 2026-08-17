"""Arming, preview and confirmation, against a real migrated Postgres.

Nothing here reaches Alpaca: the submission client is substituted. What is tested is everything
that decides WHETHER to submit, and the ordering around the submission — which is where a mistake
costs money rather than a test run.

PRECONDITIONS ARE GUARDED THE WAY test_auth_db.py GUARDS THEM, and for the reason its comment
gives: CI's backend job HAS a Docker daemon but installs only backend/requirements.txt, which does
not include testcontainers. A module-scope `from testcontainers... import` therefore kills
COLLECTION — not this file's tests, the WHOLE suite, because a collection error is fatal to the
run. That is exactly what happened on the first push of this file, and the pattern below already
existed to prevent it.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.config import get_settings
from app.services import execution as ex

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "db"))


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _testcontainers_available() -> bool:
    return importlib.util.find_spec("testcontainers") is not None


_INTEGRATION_READY = _docker_available() and _testcontainers_available()

pytestmark = pytest.mark.skipif(
    not _INTEGRATION_READY,
    reason="integration test needs docker AND the testcontainers package",
)

if _INTEGRATION_READY:  # guarded so collection succeeds without either precondition
    import psycopg
    from migrate import main as migrate_main

    try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        from testcontainers.postgres import PostgresContainer

PG_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def exec_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


@pytest.fixture()
def db(exec_pg, monkeypatch) -> Iterator[str]:
    from app.db import close_pool, reset_db_settings

    admin = (
        f"postgresql://{exec_pg.username}:{exec_pg.password}"
        f"@{exec_pg.get_container_host_ip()}:{exec_pg.get_exposed_port(5432)}/{exec_pg.dbname}"
    )
    name = f"exec_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    monkeypatch.setenv("DATABASE_URL", url)
    reset_db_settings()
    close_pool()
    assert migrate_main(["up", "--migrations-dir", str(REPO / "db" / "migrations")]) == 0
    yield url
    close_pool()
    reset_db_settings()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv("EXECUTION_ENABLED", "true")
    monkeypatch.setenv("ALPACA_API_KEY_ID", "PKTEST")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def operator(db) -> int:
    with psycopg.connect(db, autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO operators (email, password_hash) VALUES (%s, %s) RETURNING id",
            ("op@example.com", "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$b2s"),
        ).fetchone()[0]


def q(url, sql, params=()):
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


# ── the outer switch ──────────────────────────────────────────────────────────────────────────


def test_disabled_is_the_default_and_refuses_everything(db, monkeypatch, operator):
    """EXECUTION_ENABLED=false means there is no path, not a closed one. This is what ships."""
    monkeypatch.delenv("EXECUTION_ENABLED", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ex.ExecutionDisabled):
        ex.arm(operator)
    get_settings.cache_clear()


def test_enabled_without_a_broker_still_refuses(db, monkeypatch, operator):
    """Named separately from 'disabled' because the fix is entirely different."""
    monkeypatch.setenv("EXECUTION_ENABLED", "true")
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ex.ExecutionDisabled, match="no destination"):
        ex.arm(operator)
    get_settings.cache_clear()


# ── arming ────────────────────────────────────────────────────────────────────────────────────


def test_arming_is_recorded_and_expires(db, enabled, operator):
    armed = ex.arm(operator)
    assert armed.armed_by == operator
    assert armed.seconds_remaining > 0
    assert ex.current_arming() is not None
    rows = q(db, "SELECT armed_by, disarmed_at FROM execution_arming")
    assert rows == [(operator, None)]


def test_disarm_closes_the_window_and_needs_no_ceremony(db, enabled, operator):
    """An emergency control that requires confirmation is not an emergency control."""
    ex.arm(operator)
    assert ex.disarm(operator) is True
    assert ex.current_arming() is None
    assert ex.disarm(operator) is False, "disarming twice is a no-op, not an error"


def test_arming_twice_leaves_exactly_one_live_window(db, enabled, operator):
    """Two overlapping windows would make 'when does this expire' ambiguous, and the later one
    would silently extend the earlier."""
    ex.arm(operator)
    ex.arm(operator)
    live = q(db, "SELECT count(*) FROM execution_arming WHERE disarmed_at IS NULL")[0][0]
    assert live == 1
    superseded = q(db, "SELECT disarm_reason FROM execution_arming WHERE disarmed_at IS NOT NULL")
    assert superseded == [("superseded",)]


def test_an_expired_window_is_not_armed(db, enabled, operator):
    ex.arm(operator)
    with psycopg.connect(db, autocommit=True) as conn:
        # BOTH timestamps move: ck_arming_window enforces expires_at > armed_at, so backdating only
        # the expiry is rejected by the schema. That refusal is correct — a row claiming it expired
        # before it was armed is incoherent, and the constraint would not let this test fake one.
        conn.execute(
            "UPDATE execution_arming SET armed_at = now() - interval '2 hours',"
            " expires_at = now() - interval '1 hour'"
        )
    assert ex.current_arming() is None, "an expired window must not count as armed"


def test_unreadable_arming_state_fails_closed(db, enabled, operator, monkeypatch):
    """If we cannot read whether execution is permitted, it is not permitted. An unknown that
    resolves to 'allowed' is how a safety gate becomes decorative."""
    from app.db import DbUnavailable

    ex.arm(operator)

    def boom(*a, **k):
        raise DbUnavailable("unreachable", "pool down")

    monkeypatch.setattr(ex, "connection", boom)
    assert ex.current_arming() is None


# ── confirmation refuses in the right order ───────────────────────────────────────────────────


def _preview(**over):
    base = {
        "preview_id": "prev-test", "broker_env": "alpaca-paper", "account_masked": "••••I1PN",
        "symbol": "NVDA", "side": "buy", "order_type": "limit", "qty": 2.0, "limit_price": 100.0,
        "blocked": False, "requires_override": [], "unoverridable_blocks": [],
    }
    base.update(over)
    return base


def _stash(preview):
    import time as _t

    with ex._preview_lock:
        ex._previews[preview["preview_id"]] = {"preview": preview, "created_monotonic": _t.monotonic()}


def test_confirm_without_arming_refuses(db, enabled, operator):
    _stash(_preview())
    with pytest.raises(ex.NotArmed):
        ex.confirm(preview_id="prev-test", operator_id=operator)


def test_an_unknown_preview_is_refused_not_reconstructed(db, enabled, operator):
    ex.arm(operator)
    with pytest.raises(ex.PreviewNotFound):
        ex.confirm(preview_id="prev-does-not-exist", operator_id=operator)


def test_a_preview_is_single_use(db, enabled, operator, monkeypatch):
    """A double-confirm must not reach the broker twice even before client_order_id could collide."""
    ex.arm(operator)
    _stash(_preview())
    monkeypatch.setattr("src.alpaca.AlpacaClient", lambda *a, **k: object())
    monkeypatch.setattr("src.alpaca_execution.submit_order", lambda **k: {"id": "x", "status": "accepted"})
    ex.confirm(preview_id="prev-test", operator_id=operator)
    with pytest.raises(ex.PreviewNotFound):
        ex.confirm(preview_id="prev-test", operator_id=operator)


def test_a_blocked_preview_needs_a_written_override(db, enabled, operator):
    ex.arm(operator)
    _stash(_preview(blocked=True, requires_override=["cash_floor"]))
    with pytest.raises(ex.GuardrailBlocked):
        ex.confirm(preview_id="prev-test", operator_id=operator)
    _stash(_preview(blocked=True, requires_override=["cash_floor"]))
    with pytest.raises(ex.GuardrailBlocked):
        ex.confirm(preview_id="prev-test", operator_id=operator, override_reason="ok")  # too short


def test_an_unoverridable_block_cannot_be_overridden_at_all(db, enabled, operator):
    """The drawdown halt fires when the account is already losing — the moment an override is least
    likely to be made well. A reason must not clear it."""
    ex.arm(operator)
    _stash(_preview(blocked=True, unoverridable_blocks=["drawdown_halt"]))
    with pytest.raises(ex.GuardrailBlocked):
        ex.confirm(
            preview_id="prev-test", operator_id=operator,
            override_reason="I have thought about this carefully and accept the risk",
        )


# ── the ordering that matters most ────────────────────────────────────────────────────────────


def test_the_audit_row_exists_before_the_broker_is_called(db, enabled, operator, monkeypatch):
    """THE ordering. An order that vanishes between request and response must still leave evidence
    it was attempted. Writing the row after a successful submission would mean the only orders on
    record are the ones that came back."""
    ex.arm(operator)
    _stash(_preview())
    seen: dict = {}

    def submit(**kwargs):
        # Read the table from inside the broker call: the row must already be there.
        seen["rows"] = q(db, "SELECT client_order_id, submit_status FROM orders")
        return {"id": "brk-1", "status": "accepted"}

    monkeypatch.setattr("src.alpaca.AlpacaClient", lambda *a, **k: object())
    monkeypatch.setattr("src.alpaca_execution.submit_order", submit)
    ex.confirm(preview_id="prev-test", operator_id=operator)

    assert seen["rows"] == [("ww-prev-test", "submitting")], (
        "the audit row must be written, with a submitting status, BEFORE the broker call"
    )
    after = q(db, "SELECT submit_status, broker_order_id FROM orders")
    assert after == [("accepted", "brk-1")]


def test_an_uncertain_submission_is_recorded_as_unknown_not_rejected(db, enabled, operator, monkeypatch):
    """'Nobody knows' is a real state. Recording it as rejected invites a retry that duplicates;
    recording it as accepted claims a fill that may not exist."""
    from src.alpaca_execution import SubmissionUncertain

    ex.arm(operator)
    _stash(_preview())

    def submit(**kwargs):
        raise SubmissionUncertain("timed out")

    monkeypatch.setattr("src.alpaca.AlpacaClient", lambda *a, **k: object())
    monkeypatch.setattr("src.alpaca_execution.submit_order", submit)
    with pytest.raises(SubmissionUncertain):
        ex.confirm(preview_id="prev-test", operator_id=operator)

    status, err = q(db, "SELECT submit_status, submit_error FROM orders")[0]
    assert status == "unknown", "an ambiguous timeout must not be recorded as rejected"
    assert "timed out" in err


def test_the_rate_cap_refuses_and_disarms(db, enabled, operator, monkeypatch):
    """A cap that only refused would leave a runaway free to keep trying the moment it drops under."""
    monkeypatch.setenv("EXECUTION_MAX_ORDERS_PER_WINDOW", "2")
    get_settings.cache_clear()
    ex.arm(operator)
    with psycopg.connect(db, autocommit=True) as conn:
        for i in range(2):
            conn.execute(
                "INSERT INTO orders (client_order_id, preview_id, preview, broker_env,"
                " account_masked, symbol, side, order_type, time_in_force, requested_qty,"
                " limit_price, guardrails_passed) VALUES (%s,'p','{}','alpaca-paper','••••X',"
                "'NVDA','buy','limit','day',1,100,true)",
                (f"filler-{i}-{uuid.uuid4().hex[:8]}",),
            )
    _stash(_preview())
    with pytest.raises(ex.RateCapExceeded):
        ex.confirm(preview_id="prev-test", operator_id=operator)
    assert ex.current_arming() is None, "tripping the cap must DISARM, not merely refuse"
    get_settings.cache_clear()
