"""Position valuation and account roll-up — the arithmetic three routers used to duplicate.

The two defects that survived triplication are pinned here: an unpriced position blended into a
P&L percentage as if it were 0%, and an unknown weight resolving to zero.
"""

from __future__ import annotations

import pytest
from app.services.valuation import account_totals, value_position, weight_pct


def test_an_unpriced_position_has_no_derived_numbers():
    """Not zero. A position we cannot price has an unknown market value, an unknown P&L and an
    unknown weight — every one of which renders very differently from a zero."""
    v = value_position(quantity=10, average_buy_price=50.0, price=None)
    assert v.cost_basis == 500.0, "cost is knowable without a price"
    assert v.market_value is None
    assert v.unrealized_pl is None
    assert v.unrealized_pl_pct is None
    assert v.priced is False


def test_pl_percent_is_over_priced_cost_only():
    """THE DEFECT. An unpriced position used to add its cost to the denominator while its P&L
    stayed out of the numerator, which treats it as exactly 0% return and drags the headline toward
    zero on any pricing gap — a fabricated number wearing a measured one's clothes."""
    priced = value_position(10, 50.0, 55.0)        # $500 cost, +$50, +10%
    unpriced = value_position(10, 50.0, None)      # $500 cost, unknown

    totals = account_totals([priced, unpriced], cash=0.0)

    assert totals.unrealized_pl == pytest.approx(50.0)
    assert totals.unrealized_pl_pct == pytest.approx(10.0), (
        "the measurable half of the book is up 10%; blending the unpriced half in as 0% would "
        f"report 5%, got {totals.unrealized_pl_pct}"
    )
    assert totals.total_cost_basis == 1000.0, "the full book cost is still reported"
    assert totals.priced_cost_basis == 500.0
    assert totals.pl_covers_whole_book is False, "the caller must be able to say it is partial"


def test_a_fully_priced_book_reports_complete_coverage():
    totals = account_totals([value_position(10, 50.0, 55.0)], cash=100.0)
    assert totals.pl_covers_whole_book is True
    assert totals.any_unpriced is False


def test_nothing_priced_yields_unknown_pl_not_a_flat_book():
    """No position valued is not a book that went nowhere."""
    totals = account_totals([value_position(10, 50.0, None)], cash=100.0)
    assert totals.unrealized_pl is None
    assert totals.unrealized_pl_pct is None


def test_the_weight_denominator_is_priced_value_plus_cash():
    """One documented basis. account.py used live equity plus cash while reconciliation.py used the
    broker's total_value, so the same position showed different weights on two pages with nothing
    saying which basis either was."""
    totals = account_totals([value_position(10, 50.0, 55.0)], cash=450.0)
    assert totals.equity_value == pytest.approx(550.0)
    assert totals.total_value == pytest.approx(1000.0)
    assert weight_pct(550.0, totals.total_value) == pytest.approx(55.0)


def test_an_unknown_weight_stays_unknown():
    assert weight_pct(None, 1000.0) is None
    assert weight_pct(100.0, 0.0) is None, "a zero book has no meaningful share"


def test_a_free_position_has_no_percentage_return():
    """A zero cost basis cannot be up 40%; the division is undefined, not infinite."""
    v = value_position(10, 0.0, 55.0)
    assert v.unrealized_pl == pytest.approx(550.0)
    assert v.unrealized_pl_pct is None
