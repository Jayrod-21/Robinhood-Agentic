"""Alpaca client + mapping.

No network: the mapping is pure and the client is driven through a fake transport. The payload
shapes match Alpaca's documented /v2/account and /v2/positions responses — notably that every
numeric arrives as a STRING, which is the single most likely thing to go wrong silently.
"""

from __future__ import annotations

import pytest

from src import alpaca as al

# Shapes as Alpaca returns them: numbers are strings, qty is signed.
ACCOUNT = {
    "account_number": "PA3ABCDEF012",
    "equity": "10432.17",
    "long_market_value": "8200.00",
    "cash": "2232.17",
    "buying_power": "4464.34",
    "currency": "USD",
}
POSITIONS = [
    {"symbol": "AAPL", "qty": "12", "avg_entry_price": "201.44", "market_value": "3671.16"},
    {"symbol": "NVDA", "qty": "20", "avg_entry_price": "150.10", "market_value": "4503.20"},
]


@pytest.fixture(autouse=True)
def paper_env(monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", al.PAPER_BASE_URL)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "PKTESTKEYID")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret-value")


# --- mapping ----------------------------------------------------------------------------------


def test_string_numerics_become_floats():
    """Alpaca returns every numeric as a string. Passing them through would hand Pydantic a str
    where it wants a float — or string-concatenate in arithmetic, which is worse because it does
    not raise."""
    snap = al.snapshot_from_alpaca(ACCOUNT, POSITIONS)
    assert isinstance(snap["account"]["total_value"], float)
    assert isinstance(snap["positions"][0]["quantity"], float)
    assert snap["account"]["total_value"] == 10432.17
    assert snap["positions"][0]["average_buy_price"] == 201.44


def test_equity_value_is_positions_only_not_total():
    """`equity` is positions + cash; `long_market_value` is positions alone. Mapping equity into
    equity_value would double-count cash in the allocation chart."""
    snap = al.snapshot_from_alpaca(ACCOUNT, POSITIONS)
    assert snap["account"]["equity_value"] == 8200.00
    assert snap["account"]["total_value"] == 10432.17
    assert snap["account"]["cash"] == 2232.17


def test_equity_value_falls_back_when_long_market_value_is_absent():
    account = {k: v for k, v in ACCOUNT.items() if k != "long_market_value"}
    snap = al.snapshot_from_alpaca(account, POSITIONS)
    assert snap["account"]["equity_value"] == pytest.approx(10432.17 - 2232.17)


def test_short_positions_are_dropped_loudly_not_silently(caplog):
    """The snapshot contract requires quantity > 0. A short must not fail validation for the WHOLE
    snapshot — one unusual position would blank the entire dashboard — but it must not vanish
    quietly either, because it means the contract needs widening."""
    positions = [*POSITIONS, {"symbol": "TSLA", "qty": "-5", "avg_entry_price": "300.00"}]
    with caplog.at_level("WARNING"):
        snap = al.snapshot_from_alpaca(ACCOUNT, positions)
    assert [p["symbol"] for p in snap["positions"]] == ["AAPL", "NVDA"]
    assert any("TSLA" in r.getMessage() for r in caplog.records), (
        "dropping a position must be logged — a silently vanished holding is how a dashboard "
        "quietly disagrees with the account it claims to show"
    )


def test_account_number_is_masked():
    snap = al.snapshot_from_alpaca(ACCOUNT, POSITIONS)
    assert snap["account"]["number_masked"].endswith("F012")
    assert "PA3ABCDEF012" not in snap["account"]["number_masked"]


def test_source_names_the_environment():
    """The dashboard shows one account. Which one must be a fact in the payload, not an assumption
    the operator makes from context."""
    snap = al.snapshot_from_alpaca(ACCOUNT, POSITIONS)
    assert snap["source"] == "alpaca-paper"


def test_mapped_snapshot_satisfies_the_existing_contract():
    """The whole point of matching the shape: the dashboard's validator must accept it unchanged."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app.services.snapshot import AccountSnapshot

    model = AccountSnapshot.model_validate(al.snapshot_from_alpaca(ACCOUNT, POSITIONS))
    assert model.symbols == ["AAPL", "NVDA"]
    assert model.source == "alpaca-paper"


# --- paper/live safety ------------------------------------------------------------------------


def test_paper_is_the_default_when_nothing_is_configured(monkeypatch):
    """An unset endpoint must default to PAPER. The failure direction matters: guessing live would
    point real orders at a funded account."""
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    assert al.base_url_from_env() == al.PAPER_BASE_URL


def test_assert_paper_refuses_a_live_endpoint(monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", al.LIVE_BASE_URL)
    client = al.AlpacaClient()
    assert client.is_paper is False
    with pytest.raises(al.AlpacaNotPaper, match="paper"):
        client.assert_paper()


def test_assert_paper_passes_on_the_paper_endpoint():
    al.AlpacaClient().assert_paper()  # must not raise


# --- credentials ------------------------------------------------------------------------------


def test_missing_credentials_fail_with_an_actionable_message(monkeypatch):
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    with pytest.raises(al.AlpacaAuthError, match=r"backend/\.env"):
        al.load_credentials()


def test_secret_never_appears_in_an_error_message():
    client = al.AlpacaClient()
    leaked = client._redact("boom APCA-API-SECRET-KEY=test-secret-value at PKTESTKEYID")
    assert "test-secret-value" not in leaked
    assert "PKTESTKEYID" not in leaked


# --- endpoint normalization -------------------------------------------------------------------
# Alpaca's dashboard shows the endpoint WITH the version segment, so that is what gets pasted into
# backend/.env. Every one of these forms must resolve to the same paper origin.


@pytest.mark.parametrize(
    "configured",
    [
        "https://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets/",
        "https://paper-api.alpaca.markets/v2",
        "https://paper-api.alpaca.markets/v2/",
        "  https://paper-api.alpaca.markets/v2  ",
    ],
)
def test_every_form_the_dashboard_shows_normalizes_to_one_origin(configured, monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", configured)
    assert al.base_url_from_env() == al.PAPER_BASE_URL


@pytest.mark.parametrize(
    "configured",
    [
        "https://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets/v2",
        "https://paper-api.alpaca.markets/v2/",
    ],
)
def test_paper_is_recognised_whatever_form_was_pasted(configured, monkeypatch):
    """THE bug this guards. Comparing the string exactly meant a pasted `/v2` suffix made a PAPER
    endpoint fail the paper test — labelling the snapshot `alpaca-live` and making assert_paper
    refuse a paper account. A value reporting the opposite of what it is does more damage than a
    404, because nothing errors."""
    monkeypatch.setenv("ALPACA_BASE_URL", configured)
    client = al.AlpacaClient()
    assert client.is_paper is True
    client.assert_paper()
    assert al.snapshot_from_alpaca(ACCOUNT, POSITIONS)["source"] == "alpaca-paper"


def test_live_is_still_recognised_with_a_version_suffix(monkeypatch):
    """The normalization must not accidentally make everything look like paper."""
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets/v2")
    client = al.AlpacaClient()
    assert client.is_paper is False
    with pytest.raises(al.AlpacaNotPaper):
        client.assert_paper()


def test_request_path_does_not_double_the_version_segment(monkeypatch):
    """`{base}/v2/account` against a base already ending in /v2 requests /v2/v2/account -> 404."""
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
    client = al.AlpacaClient()
    assert client.base_url == al.PAPER_BASE_URL
    assert f"{client.base_url}/v2/account".count("/v2") == 1
