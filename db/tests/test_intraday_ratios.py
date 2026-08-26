"""The intraday ratio arithmetic (issue #133), and the conventions it commits to.

Formulas are the part of this system with the worst ratio of "looks obviously right" to "is right".
`pe_forward` was once mapped from `forwardPriceToEarningsGrowthRatio` — a PEG — and a bear
researcher caught it mid-debate, calling it "almost certainly a data error". Every row of the
intraday log records FORMULA_VERSION so that class of mistake stays correctable; this file pins what
version 1 actually means.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from intraday_ratios import FORMULA_VERSION, compute, fcf_yield, pe

# ── P/E ───────────────────────────────────────────────────────────────────────────────────────


def test_pe_is_price_over_earnings() -> None:
    assert pe(100, 5) == Decimal(20)


def test_a_loss_making_company_gets_a_negative_pe_not_a_null() -> None:
    """Screening convention often renders a negative P/E as "N/A". This table records what was
    true and leaves the display decision to the page — a consumer reading NULL cannot tell
    "loss-making" from "we had no EPS", and those are different facts."""
    assert pe(100, -5) == Decimal(-20)


def test_zero_earnings_is_undefined_not_infinite() -> None:
    assert pe(100, 0) is None


def test_a_missing_input_yields_none(  ) -> None:
    assert pe(None, 5) is None
    assert pe(100, None) is None


def test_string_and_decimal_inputs_both_work() -> None:
    """psycopg returns Decimal, FMP returns float or str. All three reach this function."""
    assert pe("100.00", Decimal("4")) == Decimal(25)
    assert pe(100.0, 4) == Decimal(25)


def test_a_non_finite_input_is_refused() -> None:
    """A NaN sneaking through would poison every comparison it touched SILENTLY — NaN is not equal
    to itself, so the bad row would not even surface as an outlier."""
    assert pe(float("nan"), 5) is None
    assert pe(100, float("inf")) is None
    assert fcf_yield(float("nan"), 1000) is None


def test_garbage_does_not_raise() -> None:
    assert pe("not a number", 5) is None
    assert pe(100, object()) is None


# ── FCF yield ─────────────────────────────────────────────────────────────────────────────────


def test_fcf_yield_is_free_cash_flow_over_market_cap() -> None:
    assert fcf_yield(1_000, 10_000) == Decimal("0.1")


def test_a_cash_burner_gets_a_negative_yield() -> None:
    """QBTS and SVRA in the live book both burn cash; a NULL there would hide it. Measured on the
    first real sweep: QBTS -0.01166, SVRA -0.08822."""
    assert fcf_yield(-1_000, 10_000) == Decimal("-0.1")


def test_a_zero_or_negative_market_cap_is_refused() -> None:
    assert fcf_yield(1_000, 0) is None
    assert fcf_yield(1_000, -5) is None


# ── the whole observation ─────────────────────────────────────────────────────────────────────


def test_no_statement_row_means_no_ratios() -> None:
    """The database enforces the same thing (ck_intraday_obs_ratios_have_lineage). This is the
    other half: a ratio computed from no statement row was computed from nothing."""
    assert compute(price=100, market_cap=1_000, fundamentals=None) == {
        "pe_trailing": None, "pe_forward": None, "fcf_yield": None,
    }


def test_a_statement_row_missing_one_figure_nulls_only_that_ratio() -> None:
    """Measured reality: eps_current is populated on 89 of 152 rows and eps_next_year_est on 0, so
    fcf_yield lands while pe_forward does not. Partial coverage must not null the whole row."""
    result = compute(
        price=100,
        market_cap=10_000,
        fundamentals={"eps_current": 5, "eps_next_year_est": None, "free_cash_flow": 1_000},
    )

    assert result["pe_trailing"] == Decimal(20)
    assert result["pe_forward"] is None
    assert result["fcf_yield"] == Decimal("0.1")


def test_the_formula_version_is_a_positive_integer() -> None:
    """It is written to every row with no column default, and the migration CHECKs >= 1."""
    assert isinstance(FORMULA_VERSION, int)
    assert FORMULA_VERSION >= 1


@pytest.mark.parametrize("bad", [{}, {"eps_current": None}])
def test_an_empty_or_all_null_statement_row_is_handled(bad: dict) -> None:
    result = compute(price=100, market_cap=1_000, fundamentals=bad)
    assert all(v is None for v in result.values())
