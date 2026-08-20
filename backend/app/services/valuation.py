"""What a position is worth, and what share of the book it is. One implementation, three former ones.

WHY THIS IS ITS OWN MODULE
    account.py, reconciliation.py and position.py each computed cost basis, market value, unrealized
    P&L and account weight independently. They had already diverged in two ways that reached the
    screen:

      * None-handling: one guarded `cost_basis > 0`, another used plain truthiness.
      * The weight DENOMINATOR: account.py used live equity recomputed from our own marks plus cash,
        reconciliation.py used the broker's reported total_value. Same position, two pages, two
        different weights — and nothing on either page saying which basis it was.

    A bug in this arithmetic had to be fixed in three places, which is how the unpriced-position P&L
    defect below survived: it was only ever fixed in none of them.

THE DENOMINATOR, DECIDED AND DOCUMENTED
    Account value = the market value of priced positions (from OUR marks) + cash.

    Not the broker's total_value. The numerator of every weight is a position valued at an FMP mark,
    so dividing by a total the broker computed from its own prices mixes two price sources into one
    ratio. Internally consistent and slightly different from the broker beats agreeing with the
    broker on the denominator and disagreeing on every numerator.

    It also matches what the slate is written against: SLATE.md's own table has a CASH row summing
    to 100%, so positions-plus-cash is the basis the document reconciles against.

UNPRICED IS UNKNOWN, NOT ZERO
    A position we cannot price contributes nothing to market value and nothing to P&L — but it must
    not contribute its cost to a P&L PERCENTAGE either. Doing that treats it as exactly 0% return
    and drags the headline toward zero on any pricing gap, which is a fabricated number wearing a
    measured one's clothes.
"""

from __future__ import annotations

from typing import NamedTuple


class PositionValue(NamedTuple):
    """One position, valued. Every derived field is None when the price is unknown."""

    cost_basis: float
    market_value: float | None
    unrealized_pl: float | None
    unrealized_pl_pct: float | None
    priced: bool


class AccountTotals(NamedTuple):
    equity_value: float
    """Market value of the PRICED positions. Unpriced ones are absent, not zero."""

    total_value: float
    """equity_value + cash — the weight denominator, and the basis SLATE.md is written against."""

    total_cost_basis: float
    """Cost of every position, priced or not. The book cost, which is knowable regardless."""

    priced_cost_basis: float
    """Cost of the priced positions only — the denominator the P&L percentage is honest over."""

    unrealized_pl: float | None
    unrealized_pl_pct: float | None
    any_unpriced: bool

    @property
    def pl_covers_whole_book(self) -> bool:
        """False when the P&L figures describe only part of the book, so a caller can say so."""
        return not self.any_unpriced


def value_position(quantity: float, average_buy_price: float, price: float | None) -> PositionValue:
    """Value one holding. ``price`` of None means unpriced, and every derived field follows."""
    cost_basis = quantity * average_buy_price
    if price is None:
        return PositionValue(cost_basis, None, None, None, False)

    market_value = quantity * price
    pl = market_value - cost_basis
    # A zero or negative cost basis has no meaningful percentage — a free position cannot be up 40%.
    pl_pct = (pl / cost_basis * 100.0) if cost_basis > 0 else None
    return PositionValue(cost_basis, market_value, pl, pl_pct, True)


def account_totals(values: list[PositionValue], cash: float) -> AccountTotals:
    """Roll positions up into the account view.

    The P&L percentage is over PRICED cost only. Including an unpriced position's cost in the
    denominator while excluding it from the numerator treats it as exactly 0% return: a $500
    position we could not price would drag a +8% book toward +4% and report that as measured.
    Percent-of-what is stated by `pl_covers_whole_book` rather than left to be assumed.
    """
    equity = sum(v.market_value for v in values if v.market_value is not None)
    total_cost = sum(v.cost_basis for v in values)
    priced_cost = sum(v.cost_basis for v in values if v.priced)
    pl = sum(v.unrealized_pl for v in values if v.unrealized_pl is not None)
    any_unpriced = any(not v.priced for v in values)

    return AccountTotals(
        equity_value=equity,
        total_value=equity + cash,
        total_cost_basis=total_cost,
        priced_cost_basis=priced_cost,
        # None, not 0.0, when nothing is priced: no position valued is not a flat book.
        unrealized_pl=pl if any(v.priced for v in values) else None,
        unrealized_pl_pct=(pl / priced_cost * 100.0) if priced_cost > 0 else None,
        any_unpriced=any_unpriced,
    )


def weight_pct(market_value: float | None, denominator: float) -> float | None:
    """A position's share of some total. None when either side is unknown or the total is zero."""
    if market_value is None or denominator <= 0:
        return None
    return market_value / denominator * 100.0
