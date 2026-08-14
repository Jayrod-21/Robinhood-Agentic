"""Unit tests for the Sprinkle Sauce screen and the yfinance field mapping.

Pure functions only — no network. Synthetic fundamentals exercise each gate and boundary.
"""

import math

from src.data import _safe_num, fundamentals_from_fmp
from src.screen import (
    MAX_PEG,
    MIN_FCF_YIELD,
    screen_ticker,
    tier1_liquidity,
    tier2_sprinkle_sauce,
)


def good_fundamentals(**overrides) -> dict:
    """A baseline ticker that passes every gate; override fields to test failures."""
    base = {
        "market_cap": 50_000_000_000,
        "peg": 1.2,
        "fcf_yield": 5.0,
        "net_income": 1_000_000_000,
        "operating_cash_flow": 1_500_000_000,
        "gross_margin": 0.45,
        "revenue_growth": 0.08,
    }
    base.update(overrides)
    return base


# --- Tier 1 ---------------------------------------------------------------------------

def test_tier1_passes_large_cap():
    assert tier1_liquidity(good_fundamentals()).passed


def test_tier1_fails_below_floor():
    res = tier1_liquidity(good_fundamentals(market_cap=1_000_000_000))
    assert not res.passed
    assert "market_cap" in res.reasons[0]


def test_tier1_relaxed_floor_lets_midcap_through():
    res = tier1_liquidity(good_fundamentals(market_cap=3_000_000_000),
                          min_market_cap=2_000_000_000)
    assert res.passed


def test_tier1_missing_market_cap():
    res = tier1_liquidity({"market_cap": None})
    assert not res.passed
    assert "unavailable" in res.reasons[0]


# --- Tier 2 ---------------------------------------------------------------------------

def test_tier2_passes_clean_name():
    assert tier2_sprinkle_sauce(good_fundamentals()).passed


def test_tier2_rejects_high_peg():
    res = tier2_sprinkle_sauce(good_fundamentals(peg=MAX_PEG + 0.5))
    assert not res.passed
    assert any("PEG" in r for r in res.reasons)


def test_tier2_rejects_negative_peg():
    res = tier2_sprinkle_sauce(good_fundamentals(peg=-1.0))
    assert not res.passed
    assert any("negative earnings growth" in r for r in res.reasons)


def test_tier2_rejects_thin_fcf_yield():
    res = tier2_sprinkle_sauce(good_fundamentals(fcf_yield=MIN_FCF_YIELD - 0.5))
    assert not res.passed
    assert any("FCF yield" in r for r in res.reasons)


def test_tier2_piotroski_proportional_pass():
    # 3 of 5 available signals pass -> 0.6 >= 5/9, should pass the Piotroski gate.
    res = tier2_sprinkle_sauce(good_fundamentals(
        net_income=1, operating_cash_flow=2, gross_margin=0.4, revenue_growth=-0.1,
    ))
    assert res.passed


def test_tier2_piotroski_proportional_fail():
    # Only 1 of 4 available signals passes -> 0.25 < 5/9, should fail.
    res = tier2_sprinkle_sauce(good_fundamentals(
        net_income=-1, operating_cash_flow=-2, gross_margin=-0.1, revenue_growth=-0.1,
        peg=1.0, fcf_yield=5.0,
    ))
    assert not res.passed
    assert any("Piotroski" in r for r in res.reasons)


def test_tier2_piotroski_no_signals():
    res = tier2_sprinkle_sauce({"peg": 1.0, "fcf_yield": 5.0})
    assert not res.passed
    assert any("no computable signals" in r for r in res.reasons)


# --- Full screen ----------------------------------------------------------------------

def test_screen_ticker_pass_has_composite():
    res = screen_ticker("TEST", good_fundamentals())
    assert res.passed and res.failed_tier is None
    assert res.composite is not None and res.composite > 0


def test_screen_ticker_stops_at_liquidity():
    res = screen_ticker("TEST", good_fundamentals(market_cap=1))
    assert not res.passed
    assert res.failed_tier == "liquidity"
    assert "sprinkle_sauce" not in res.tiers  # short-circuited


def test_screen_ranks_cheaper_higher():
    cheap = screen_ticker("CHEAP", good_fundamentals(peg=0.5, fcf_yield=9.0))
    rich = screen_ticker("RICH", good_fundamentals(peg=1.9, fcf_yield=3.1))
    assert cheap.composite > rich.composite


# --- Data mapping ---------------------------------------------------------------------
# The yfinance mapping tests that lived here were deleted with the function they covered. Their
# coverage did not vanish: tests/test_fmp.py::test_fcf_yield_is_a_percentage_and_recomputable_by_hand
# and ::test_missing_market_cap_returns_none_not_a_zeroed_row assert the same two properties against
# the FMP mapping — and against a REAL captured payload rather than a hand-built dict, which is
# strictly stronger.
def test_safe_num_rejects_nan_and_non_numeric():
    """_safe_num survived the provider swap and still guards every mapped field. NaN must become
    None rather than propagate: a NaN margin compares false against every gate threshold, so the
    name would be rejected for failing a test it was never actually measured against."""
    assert _safe_num(math.nan) is None
    assert _safe_num(None) is None
    assert _safe_num("not-a-number") is None
    assert _safe_num("3.5") == 3.5
    assert _safe_num(0) == 0.0


def test_nan_in_a_provider_payload_becomes_none_not_a_gate_failure():
    """End-to-end through the live mapping: a NaN margin from the provider must arrive as None."""
    bundle = {
        "profile": {"symbol": "X", "marketCap": 1e9},
        "ratios": {"grossProfitMargin": math.nan},
        "income": {}, "cash_flow": {}, "growth": {},
    }
    assert fundamentals_from_fmp(bundle)["gross_margin"] is None
