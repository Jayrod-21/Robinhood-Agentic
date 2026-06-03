"""Unit tests for the Sprinkle Sauce screen and the yfinance field mapping.

Pure functions only — no network. Synthetic fundamentals exercise each gate and boundary.
"""

import math

import pytest

from src.data import fundamentals_from_info
from src.screen import (
    MAX_PEG,
    MIN_FCF_YIELD,
    MIN_MARKET_CAP,
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

def test_fundamentals_from_info_computes_fcf_yield():
    data = fundamentals_from_info({
        "marketCap": 1_000_000_000,
        "freeCashflow": 50_000_000,
        "trailingPegRatio": 1.5,
    })
    assert data["fcf_yield"] == pytest.approx(5.0)
    assert data["peg"] == 1.5


def test_fundamentals_from_info_handles_nan_and_missing():
    data = fundamentals_from_info({"marketCap": float("nan"), "pegRatio": None})
    assert data["market_cap"] is None
    assert data["peg"] is None
    assert data["fcf_yield"] is None  # cannot compute without market cap


def test_fundamentals_from_info_peg_fallback():
    data = fundamentals_from_info({"marketCap": 1e9, "pegRatio": 2.2})
    assert data["peg"] == 2.2  # falls back to legacy field when trailing missing


def test_safe_num_rejects_nan():
    data = fundamentals_from_info({"marketCap": 1e9, "grossMargins": math.nan})
    assert data["gross_margin"] is None
