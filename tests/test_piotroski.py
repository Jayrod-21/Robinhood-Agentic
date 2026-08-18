"""Cary's F-Score.

The formula came from a Bloomberg sheet, so these tests are written against that formula's terms
rather than against the textbook — including the two signals where the two definitions disagree,
because that disagreement is the whole reason `variant` is stored.
"""

from __future__ import annotations

from src import piotroski


def _period(**over):
    base = dict(
        period_end="2025-12-31", net_income=100.0, cfo=150.0, shares_out=1000.0,
        total_assets=1000.0, long_term_debt=200.0, current_assets=300.0,
        current_liabilities=150.0, cost_of_revenue=400.0, revenue=1000.0,
    )
    base.update(over)
    return base


def test_a_company_improving_on_every_axis_scores_nine():
    prior = _period(period_end="2024-12-31")
    current = _period(
        net_income=200.0,          # up
        cfo=300.0,                 # up, and above net income
        shares_out=900.0,          # buyback
        long_term_debt=100.0,      # deleveraging
        current_assets=450.0,      # liquidity up
        cost_of_revenue=350.0,     # margin up
        revenue=1200.0,            # turnover up
    )
    r = piotroski.score(current, prior)
    assert r["score"] == 9
    assert r["complete"] is True
    assert r["variant"] == "cary"
    assert all(v is True for v in r["signals"].values())


def test_a_company_deteriorating_on_every_axis_scores_zero():
    prior = _period(period_end="2024-12-31")
    current = _period(
        net_income=50.0, cfo=25.0, shares_out=1200.0, long_term_debt=400.0,
        current_assets=150.0, cost_of_revenue=600.0, revenue=800.0,
    )
    r = piotroski.score(current, prior)
    assert r["score"] == 0
    assert r["complete"] is True


def test_the_two_signals_where_carys_variant_differs_from_the_textbook():
    """THE reason `variant` is stored. A company with NEGATIVE but IMPROVING net income and cash
    flow earns both points here; Piotroski (1998) tests positivity and would award neither.

    A two-point swing on a nine-point scale, on exactly the distressed names the score is meant to
    discriminate between."""
    prior = _period(period_end="2024-12-31", net_income=-300.0, cfo=-200.0)
    current = _period(net_income=-100.0, cfo=-50.0)  # still negative, both improving
    r = piotroski.score(current, prior)
    assert r["signals"]["net_income_improved"] is True
    assert r["signals"]["cfo_improved"] is True
    assert r["variant"] == "cary", "the score is meaningless without the definition that produced it"


def test_a_missing_input_is_unknown_not_a_failed_signal():
    """Scoring an absent input as zero punishes a company for a gap in OUR data, and the number
    then looks like a judgement about the business."""
    prior = _period(period_end="2024-12-31")
    # total_assets is the denominator of THREE signals — ROA, leverage, and asset turnover — so
    # losing it costs three, not one. Worth stating: the first version of this test expected 7 and
    # the code was right.
    current = _period(total_assets=None)
    r = piotroski.score(current, prior)
    assert r["signals"]["roa_improved"] is None
    assert r["signals"]["leverage_fell"] is None
    assert r["signals"]["asset_turnover_improved"] is None
    assert r["complete"] is False
    assert r["evaluated"] == 6
    assert r["score"] <= 6, "unknown signals must not be counted as passes"


def test_score_and_evaluated_are_reported_together():
    """4 of 9 computed from six available signals is a different claim from 4 of 9 computed from
    all nine, and the consumer must be able to tell them apart."""
    prior = _period(period_end="2024-12-31")
    current = _period(cfo=None, shares_out=None)
    r = piotroski.score(current, prior)
    assert r["of"] == 9
    assert r["evaluated"] < 9
    assert r["complete"] is False


def test_a_zero_denominator_does_not_raise_or_score():
    """A company with no assets on record is a data problem, not a nine-signal failure."""
    prior = _period(period_end="2024-12-31", total_assets=0.0, revenue=0.0)
    current = _period(total_assets=0.0, revenue=0.0)
    r = piotroski.score(current, prior)
    assert r["signals"]["roa_improved"] is None
    assert r["signals"]["gross_margin_improved"] is None


def test_the_signal_names_match_the_bloomberg_formula_order():
    """Nine signals, in the order Cary's formula concatenates them — so a reviewer can read the two
    side by side."""
    r = piotroski.score(_period(), _period(period_end="2024-12-31"))
    assert list(r["signals"]) == [
        "net_income_improved", "cfo_improved", "shares_not_diluted", "cfo_exceeds_net_income",
        "roa_improved", "leverage_fell", "current_ratio_improved", "gross_margin_improved",
        "asset_turnover_improved",
    ]
