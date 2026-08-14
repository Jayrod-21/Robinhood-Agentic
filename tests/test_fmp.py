"""FMP client + mapping tests.

Every payload here is a REAL response captured from FMP's /stable/ API on 2026-08-14 (AAPL,
fixtures in tests/fixtures/fmp/). Nothing in this file touches the network: the mapping is pure,
and the client is exercised against a fake transport. That matters more than usual here — the whole
reason this module exists is that the previous data source returned a shape nobody had pinned, and
a fixture captured from the live API is the only kind of test that would have caught the /api/v3
retirement or the fraction-vs-percent mismatch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.fmp as fmp_mod
from src.data import fundamentals_from_fmp, known_at_from_statement
from src.fmp import (
    CallBudget,
    FmpBudgetExhausted,
    FmpClient,
    budget_from_env,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fmp"


def _fixture(name: str):
    rows = json.loads((FIXTURES / f"{name}.json").read_text())
    return rows[0] if isinstance(rows, list) and rows else rows


@pytest.fixture()
def bundle() -> dict:
    return {
        "profile": _fixture("profile"),
        "ratios": _fixture("ratios"),
        "income": _fixture("income-statement"),
        "cash_flow": _fixture("cash-flow-statement"),
        "growth": _fixture("financial-growth"),
    }


# --- mapping ----------------------------------------------------------------------------------


def test_maps_every_field_the_screen_gates_on(bundle):
    """The gates are only as good as the mapping under them: a field that silently arrives None
    reads as 'this company failed the screen' rather than 'we did not fetch it'."""
    f = fundamentals_from_fmp(bundle)
    for key in (
        "market_cap",
        "peg",
        "fcf_yield",
        "free_cash_flow",
        "net_income",
        "operating_cash_flow",
        "gross_margin",
        "revenue_growth",
    ):
        assert f[key] is not None, f"{key} did not map from a real FMP payload"


def test_margins_and_growth_stay_fractions_matching_the_old_source(bundle):
    """yfinance returned grossMargins/revenueGrowth as fractions and the gate thresholds were
    written against that. FMP also returns fractions, so these must pass through UNSCALED — a
    stray *100 here would silently pass every margin gate."""
    f = fundamentals_from_fmp(bundle)
    assert 0.0 < f["gross_margin"] < 1.0, "gross_margin must stay a fraction, as the gates expect"
    assert -1.0 < f["revenue_growth"] < 1.0, "revenue_growth must stay a fraction"


def test_fcf_yield_is_a_percentage_and_recomputable_by_hand(bundle):
    """The screen spec defines fcf_yield as a percent (3.0 == 3%), FMP does not supply it, and the
    derivation must match the yfinance path it replaces. Checked by recomputing from the inputs."""
    f = fundamentals_from_fmp(bundle)
    expected = bundle["cash_flow"]["freeCashFlow"] / bundle["profile"]["marketCap"] * 100.0
    assert f["fcf_yield"] == pytest.approx(expected)
    assert f["fcf_yield"] > 1.0, "a percent, not the 0.0xx fraction"


def test_missing_market_cap_returns_none_not_a_zeroed_row(bundle):
    """Contract match with fetch_fundamentals: no market cap means the tiered gates cannot run, so
    the row must be absent rather than present-and-ungateable."""
    bundle["profile"] = {**bundle["profile"], "marketCap": None}
    assert fundamentals_from_fmp(bundle) is None


def test_mapping_survives_entirely_empty_sections(bundle):
    """A symbol with no filed statements still has a profile. The screen must get a row it can
    reject on missing gates, not an exception that kills the whole scan."""
    sparse = {**bundle, "ratios": None, "income": None, "cash_flow": None, "growth": None}
    f = fundamentals_from_fmp(sparse)
    assert f is not None and f["market_cap"] is not None
    assert f["peg"] is None and f["fcf_yield"] is None


# --- point-in-time dating ---------------------------------------------------------------------


def test_known_at_is_the_acceptance_date_not_the_period_end():
    """THE look-ahead test. AAPL's FY2025 period ended 2025-09-27; the filing was accepted
    2025-10-31. Using the period end would let a backtest read the numbers five weeks early."""
    income = _fixture("income-statement")
    known_at = known_at_from_statement(income)
    assert known_at is not None
    assert known_at.startswith("2025-10-31"), known_at
    assert not known_at.startswith(income["date"]), "known_at must not be the period end"


def test_known_at_falls_back_to_filing_date_then_none():
    assert known_at_from_statement({"filingDate": "2025-10-31"}) == "2025-10-31"
    # No date at all: None, so the row stays out of point-in-time queries rather than guessing.
    assert known_at_from_statement({"date": "2025-09-27"}) is None
    assert known_at_from_statement({}) is None


# --- call budget ------------------------------------------------------------------------------


def test_budget_refuses_past_the_limit_and_counts_what_it_spent():
    b = CallBudget(limit=3)
    for _ in range(3):
        b.take()
    assert b.remaining() == 0
    with pytest.raises(FmpBudgetExhausted) as exc:
        b.take()
    assert "3/3" in str(exc.value), "the refusal must say how much was spent, not just refuse"


def test_daily_cap_defaults_to_off_because_the_plan_meters_per_minute(monkeypatch):
    """The Starter plan meters 300/min with NO daily cap. An invented daily ceiling would refuse
    work the owner paid for — the earlier version of this module defaulted to 240/day, which would
    have stopped a scan after 48 symbols on a plan allowing hundreds of thousands."""
    monkeypatch.delenv("FMP_DAILY_CALL_BUDGET", raising=False)
    assert budget_from_env().limit == 0, "no daily cap unless one is deliberately configured"
    monkeypatch.setenv("FMP_DAILY_CALL_BUDGET", "500")
    assert budget_from_env().limit == 500, "an explicit cap is still honoured"
    monkeypatch.setenv("FMP_DAILY_CALL_BUDGET", "not-a-number")
    assert budget_from_env().limit == 0, "garbage falls back to the default, never to a guess"


def test_rate_gate_paces_instead_of_refusing(monkeypatch):
    """A rate limit is a speed limit, not a quota: the work is allowed, it just has to arrive
    slower. Refusing here would kill a long backfill partway through."""
    slept: list[float] = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(fmp_mod.time, "monotonic", lambda: clock["t"])

    def fake_sleep(s):
        slept.append(s)
        clock["t"] += s

    monkeypatch.setattr(fmp_mod.time, "sleep", fake_sleep)
    gate = fmp_mod.MinuteRateGate(calls_per_minute=3)
    for _ in range(3):
        gate.acquire()
    assert slept == [], "the first three must not wait"
    gate.acquire()  # fourth in the same minute
    assert slept and slept[0] > 0, "the fourth must WAIT, not raise"
    assert gate.waited_total_s > 0, "pacing must be observable, not silent"


def test_rate_gate_gives_up_rather_than_hanging_forever(monkeypatch):
    """A wedged clock or an external caller saturating the plan must not hang a job indefinitely."""
    monkeypatch.setattr(fmp_mod.time, "monotonic", lambda: 1000.0)  # time never advances
    monkeypatch.setattr(fmp_mod.time, "sleep", lambda s: None)
    gate = fmp_mod.MinuteRateGate(calls_per_minute=1, max_wait_s=5.0)
    gate.acquire()
    with pytest.raises(fmp_mod.FmpError, match="rate gate"):
        gate.acquire()


def test_bundle_refuses_rather_than_fetching_a_partial_symbol(monkeypatch):
    """A symbol half-fetched because the budget ran out mid-way would be written with missing
    gates — indistinguishable from a company that genuinely failed the screen."""
    client = FmpClient(api_key="test-key", budget=CallBudget(limit=3))
    calls: list[str] = []
    monkeypatch.setattr(client, "get", lambda ep, params=None: calls.append(ep) or [])
    with pytest.raises(FmpBudgetExhausted):
        client.fundamentals_bundle("AAPL")
    assert calls == [], "not one call may leave the process when the bundle cannot complete"


def test_api_key_never_appears_in_an_error_message():
    """The key is a paid credential; an exception string ends up in logs and tickets."""
    secret = "super-secret-key-value"
    client = FmpClient(api_key=secret, budget=CallBudget(limit=10))
    assert secret not in client._redact(f"connection to host?apikey={secret} failed")
    assert "<FMP_KEY>" in client._redact(f"boom apikey={secret}")
