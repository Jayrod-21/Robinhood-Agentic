"""Which account the dashboard shows, and how it behaves when it cannot tell.

The selection rule is one line of code and the most consequential decision in this change: when
Alpaca is configured but unreachable, the dashboard REFUSES rather than falling back to the file.

That refusal was originally justified by the file being a Robinhood export from a different broker,
months out of date. It is not that any more — bin/alpaca_snapshot.py rewrites it from Alpaca every
minute, so the fallback now holds the same broker's positions, typically seconds old.

The refusal STANDS anyway, for the reason that outlives the file's staleness: an outage the operator
cannot see is worse than one they can. A page that quietly switches to a cached book during a broker
outage looks identical to a working one, and every number on it is a claim about the present made
from the past. The fresher fallback lowers the cost of that mistake; it does not make it not a
mistake.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services import broker
from app.services.snapshot import SnapshotError

FILE_SNAPSHOT = Path(__file__).resolve().parents[2] / "data" / "account_snapshot.json"

ALPACA_PAYLOAD = {
    "schema_version": 1,
    "source": "alpaca-paper",
    "generated_at": "2026-08-17T12:00:00Z",
    "account": {
        "number_masked": "••••I1PN",
        "nickname": "Alpaca paper",
        "total_value": 100000.0,
        "equity_value": 0.0,
        "cash": 100000.0,
        "buying_power": 100000.0,
        "currency": "USD",
    },
    "positions": [],
}


@pytest.fixture(autouse=True)
def clean_cache():
    broker.reset_cache()
    yield
    broker.reset_cache()


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "PKTEST")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")


@pytest.fixture()
def unconfigured(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)


def test_broker_is_preferred_when_configured(configured, monkeypatch):
    monkeypatch.setattr(broker, "_fetch_alpaca", lambda: broker.AccountSnapshot.model_validate(ALPACA_PAYLOAD))
    snap = broker.get_snapshot(FILE_SNAPSHOT)
    assert snap.source == "alpaca-paper"


def test_file_is_used_when_the_broker_is_not_configured(unconfigured, tmp_path, monkeypatch):
    """The pre-Alpaca posture must keep working — this is not a forced migration.

    Proves the FILE was read by giving it a timestamp nothing else could produce. The previous
    version asserted ``source != "alpaca-paper"``, which conflated "came from the file" with "is not
    Alpaca data". Those stopped being the same thing the moment bin/alpaca_snapshot.py began writing
    the fallback FROM Alpaca — so the test failed for the one reason it never should have: the
    fallback got better. It also read the real data/account_snapshot.json and skipped when absent,
    which meant it silently tested nothing on any machine without one.
    """
    called: list[int] = []
    monkeypatch.setattr(broker, "_fetch_alpaca", lambda: called.append(1))

    path = tmp_path / "account_snapshot.json"
    path.write_text(json.dumps({**ALPACA_PAYLOAD, "generated_at": "2001-01-01T00:00:00Z"}))

    snap = broker.get_snapshot(path)
    assert snap.generated_at == "2001-01-01T00:00:00Z", "the account did not come from the file"
    assert not called, "the broker was called even though no credentials are configured"


def test_half_a_credential_is_not_configured(monkeypatch):
    """Half a credential is a misconfiguration, not a source. Treating the key alone as configured
    sends a request that 401s, which reads as 'Alpaca is broken' rather than 'the secret is
    missing from backend/.env'."""
    monkeypatch.setenv("ALPACA_API_KEY_ID", "PKTEST")
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    assert broker.alpaca_configured() is False
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "   ")
    assert broker.alpaca_configured() is False


def test_broker_failure_refuses_and_never_serves_the_other_brokers_file(configured, monkeypatch):
    """THE decision. An outage is recoverable; a dashboard confidently showing the wrong account is
    how someone acts on a position that does not exist."""
    from src.alpaca import AlpacaError

    def boom():
        raise AlpacaError("connection reset")

    monkeypatch.setattr("src.alpaca.fetch_snapshot", lambda *a, **k: boom())
    with pytest.raises(SnapshotError) as exc:
        broker.get_snapshot(FILE_SNAPSHOT)
    message = str(exc.value)
    assert "stale holdings" in message, "the refusal must say WHY it refused"
    assert "robinhood" not in message.lower(), "must not leak the other source as a suggestion"


def test_a_broker_outage_does_not_serve_a_stale_cached_snapshot(configured, monkeypatch):
    """A cache that outlives the connection is a fallback wearing a different hat.

    Patched at src.alpaca.fetch_snapshot for BOTH phases, not at broker._fetch_alpaca: patching the
    wrapper for phase one would leave it patched for phase two, so the outage never reaches the
    code under test and the assertion passes without exercising anything."""
    from src import alpaca as alpaca_mod

    state = {"fail": False}

    def transport(*_a, **_k):
        if state["fail"]:
            raise alpaca_mod.AlpacaError("down")
        return dict(ALPACA_PAYLOAD)

    monkeypatch.setattr(alpaca_mod, "fetch_snapshot", transport)
    assert broker.get_snapshot(FILE_SNAPSHOT).source == "alpaca-paper"

    broker.reset_cache()
    state["fail"] = True
    with pytest.raises(SnapshotError):
        broker.get_snapshot(FILE_SNAPSHOT)


def test_cache_prevents_a_fetch_per_poll(configured, monkeypatch):
    """/api/account is polled every 10s per tab per operator, and each miss is two Alpaca calls."""
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return broker.AccountSnapshot.model_validate(ALPACA_PAYLOAD)

    monkeypatch.setattr(broker, "_fetch_alpaca", counted)
    for _ in range(5):
        broker.get_snapshot(FILE_SNAPSHOT)
    assert calls["n"] == 1, "five polls inside the TTL must cost one fetch"
