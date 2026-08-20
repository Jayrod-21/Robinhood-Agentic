"""The Data-Trust strip's endpoint.

This route exists because the dashboard spent three weeks showing 27 July holdings as current with
nothing on screen saying so. So the tests here are mostly about the endpoint refusing to claim
freshness it cannot prove — a strip that renders green when it does not know is worse than no strip,
because it converts an unanswered question into a wrong answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from app.routers import data_trust as dt
from app.services.marks import Mark
from app.services.snapshot import AccountSnapshot, SnapshotError


def _snapshot(generated_at: str, positions=("AAPL", "NVDA"), source="alpaca-paper"):
    return AccountSnapshot.model_validate(
        {
            "schema_version": 1,
            "source": source,
            "generated_at": generated_at,
            "account": {
                "number_masked": "••••I1PN",
                "total_value": 1000.0,
                "equity_value": 600.0,
                "cash": 400.0,
                "buying_power": 400.0,
            },
            "positions": [
                {"symbol": s, "quantity": 1.0, "average_buy_price": 10.0} for s in positions
            ],
        }
    )


def _iso(dt_: datetime) -> str:
    return dt_.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def fresh(monkeypatch):
    monkeypatch.setattr(dt, "get_snapshot", lambda _p, _acct=None: _snapshot(_iso(datetime.now(timezone.utc))))
    monkeypatch.setattr(dt, "get_marks_detailed", lambda syms, ttl: {s: Mark(100.0, False, 0.0) for s in syms})


def test_fresh_broker_read_is_not_stale(fresh):
    body = dt.data_trust()
    assert body["snapshot_stale"] is False
    assert body["positions_total"] == 2 and body["positions_priced"] == 2
    assert body["prices_degraded"] is False
    assert body["source"] == "alpaca-paper"


def test_an_old_snapshot_is_reported_stale(monkeypatch):
    """THE case this endpoint was built for: 27 July rendering as current."""
    old = datetime.now(timezone.utc) - timedelta(days=21)
    monkeypatch.setattr(dt, "get_snapshot", lambda _p, _acct=None: _snapshot(_iso(old), source="robinhood-mcp"))
    monkeypatch.setattr(dt, "get_marks_detailed", lambda syms, ttl: {s: Mark(100.0, False, 0.0) for s in syms})
    body = dt.data_trust()
    assert body["snapshot_stale"] is True, "a three-week-old snapshot must not read as fresh"
    assert body["source"] == "robinhood-mcp"


def test_an_unparseable_timestamp_is_stale_not_fresh(monkeypatch):
    """Freshness must be PROVEN. Defaulting an unreadable stamp to fresh is how the July snapshot
    went unremarked for three weeks."""
    monkeypatch.setattr(dt, "get_snapshot", lambda _p, _acct=None: _snapshot("not-a-timestamp"))
    monkeypatch.setattr(dt, "get_marks_detailed", lambda syms, ttl: {s: Mark(100.0, False, 0.0) for s in syms})
    assert dt.data_trust()["snapshot_stale"] is True


def test_partially_priced_positions_are_reported_degraded(monkeypatch):
    """Eight held, six priced means two are showing something other than a live mark. Today nothing
    tells the operator which — this is the count that surfaces it."""
    monkeypatch.setattr(dt, "get_snapshot", lambda _p, _acct=None: _snapshot(_iso(datetime.now(timezone.utc))))
    monkeypatch.setattr(dt, "get_marks_detailed",
                        lambda syms, ttl: {"AAPL": Mark(100.0, False, 0.0), "NVDA": Mark(None, False, None)})
    body = dt.data_trust()
    assert body["positions_total"] == 2
    assert body["positions_priced"] == 1
    assert body["prices_degraded"] is True


def test_no_account_data_returns_200_with_an_honest_empty_state(monkeypatch):
    """'We have no fresh data' is a fact the strip must render. It is a DIFFERENT state from the
    endpoint being unreachable, and collapsing them leaves the operator unable to tell a broker
    outage from a dead site."""
    def boom(_p, _acct=None):
        raise SnapshotError("broker unreachable")

    monkeypatch.setattr(dt, "get_snapshot", boom)
    body = dt.data_trust()
    assert body["snapshot_generated_at"] is None
    assert body["snapshot_stale"] is True
    assert body["source"] is None
    assert body["positions_total"] == 0 and body["positions_priced"] == 0
    assert body["prices_degraded"] is True


def test_price_source_comes_from_the_module_that_fetches(fresh):
    """A literal in this route would be true until the provider changes, then a lie on the page
    whose whole job is telling the operator what to trust — which is exactly what /api/health did
    with the broker's account number."""
    from app.services.marks import MARKS_PROVIDER

    assert dt.data_trust()["price_source"] == MARKS_PROVIDER


def test_empty_portfolio_prices_nothing_and_is_not_degraded(monkeypatch):
    """A funded-but-unspent account (the current Alpaca paper state) is honest, not broken."""
    monkeypatch.setattr(
        dt, "get_snapshot", lambda _p, _acct=None: _snapshot(_iso(datetime.now(timezone.utc)), positions=())
    )
    called = {"n": 0}

    def marks(syms, ttl):
        called["n"] += 1
        return {}

    monkeypatch.setattr(dt, "get_marks_detailed", marks)
    body = dt.data_trust()
    assert body["positions_total"] == 0
    assert body["prices_degraded"] is False, "no positions is not the same as unpriced positions"
    assert called["n"] == 0, "no held names must cost no marks lookup"


def test_posture_flags_are_present(fresh):
    body = dt.data_trust()
    assert body["returns_basis"] == "price_only"
    assert isinstance(body["auth_enforced"], bool)
    assert isinstance(body["debate_live"], bool)
