"""The Piotroski F-Score, in the variant this project actually uses.

WHICH DEFINITION, AND WHY IT MATTERS
    Two definitions are in circulation here and they are NOT interchangeable.

    Piotroski (1998) scores nine binary signals, two of which test LEVELS: is ROA positive, is
    operating cash flow positive.

    Cary's version — the one the owner's professor wrote in Bloomberg, and the one the validation
    export was generated with — replaces those two with IMPROVEMENT tests: did net income rise year
    over year, did operating cash flow rise year over year. The other seven match.

    The disagreement is not academic. A company with negative but improving earnings scores 2 points
    under Cary's and 0 under Piotroski (1998) — a two-point swing on a nine-point scale, on exactly
    the distressed names where the score is supposed to be discriminating. So every stored score
    carries `variant`, and the two must never be compared without it.

    Implemented: CARY. Verbatim from the Bloomberg formula:

        IF(NetIncome:Y > NetIncome:Y-1)          + IF(CFO:Y > CFO:Y-1)
      + IF(SharesOut:Y < SharesOut:Y-1)          + IF(CFO:Y > NetIncome:Y)
      + IF(NI/TA:Y > NI/TA:Y-1)                  + IF(LTD/TA:Y < LTD/TA:Y-1)
      + IF(CA/CL:Y > CA/CL:Y-1)                  + IF(COGS/Rev:Y < COGS/Rev:Y-1)
      + IF(Rev/TA:Y > Rev/TA:Y-1)

MISSING INPUTS DO NOT SCORE ZERO
    A signal whose inputs are absent is UNKNOWN, not failed. Scoring it zero would quietly punish a
    company for a gap in our data, and the resulting number would look like a judgement about the
    business. `signals` records each as true/false/None, and `score` counts only the trues while
    `evaluated` says how many of the nine could actually be tested — so a 4/9 computed from six
    available signals is visibly different from a 4/9 computed from all nine.
"""

from __future__ import annotations

from typing import Any

VARIANT = "cary"
SIGNAL_COUNT = 9


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _ratio(numerator: Any, denominator: Any) -> float | None:
    n, d = _num(numerator), _num(denominator)
    if n is None or d is None or d == 0:
        return None
    return n / d


def _cmp(a: float | None, b: float | None, *, greater: bool) -> bool | None:
    """True/False when both sides are known, None when either is missing."""
    if a is None or b is None:
        return None
    return (a > b) if greater else (a < b)


def score(current: dict, prior: dict) -> dict[str, Any]:
    """Cary's F-Score from two consecutive annual periods.

    Each period dict needs: net_income, cfo, shares_out, total_assets, long_term_debt,
    current_assets, current_liabilities, cost_of_revenue, revenue.
    """
    c, p = current, prior

    signals: dict[str, bool | None] = {
        # 1. Profitability improving (Cary; classic tests ROA > 0)
        "net_income_improved": _cmp(_num(c.get("net_income")), _num(p.get("net_income")), greater=True),
        # 2. Operating cash flow improving (Cary; classic tests CFO > 0)
        "cfo_improved": _cmp(_num(c.get("cfo")), _num(p.get("cfo")), greater=True),
        # 3. No dilution
        "shares_not_diluted": _cmp(_num(c.get("shares_out")), _num(p.get("shares_out")), greater=False),
        # 4. Earnings quality: cash beats accounting profit
        "cfo_exceeds_net_income": _cmp(_num(c.get("cfo")), _num(c.get("net_income")), greater=True),
        # 5. Return on assets improving
        "roa_improved": _cmp(
            _ratio(c.get("net_income"), c.get("total_assets")),
            _ratio(p.get("net_income"), p.get("total_assets")), greater=True),
        # 6. Leverage falling
        "leverage_fell": _cmp(
            _ratio(c.get("long_term_debt"), c.get("total_assets")),
            _ratio(p.get("long_term_debt"), p.get("total_assets")), greater=False),
        # 7. Liquidity improving
        "current_ratio_improved": _cmp(
            _ratio(c.get("current_assets"), c.get("current_liabilities")),
            _ratio(p.get("current_assets"), p.get("current_liabilities")), greater=True),
        # 8. Gross margin improving (cost of revenue falling as a share of revenue)
        "gross_margin_improved": _cmp(
            _ratio(c.get("cost_of_revenue"), c.get("revenue")),
            _ratio(p.get("cost_of_revenue"), p.get("revenue")), greater=False),
        # 9. Asset turnover improving
        "asset_turnover_improved": _cmp(
            _ratio(c.get("revenue"), c.get("total_assets")),
            _ratio(p.get("revenue"), p.get("total_assets")), greater=True),
    }

    evaluated = sum(1 for v in signals.values() if v is not None)
    total = sum(1 for v in signals.values() if v is True)

    return {
        "score": total,
        "variant": VARIANT,
        "evaluated": evaluated,
        "of": SIGNAL_COUNT,
        # Complete, not "complete enough": a score built from six of nine signals is a different
        # claim from one built from all nine, and the consumer must be able to tell.
        "complete": evaluated == SIGNAL_COUNT,
        "signals": signals,
        "periods": {"current": c.get("period_end"), "prior": p.get("period_end")},
    }
