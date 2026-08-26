"""The arithmetic of the intraday ratio log, isolated so it can be versioned and tested.

WHY THIS IS ITS OWN MODULE
    `pe_forward` was once mapped from `forwardPriceToEarningsGrowthRatio` — a PEG — and a bear
    researcher caught it mid-debate. Formulas are the part of this system with the worst
    ratio of "looks obviously right" to "is right", so they live in one file, take plain numbers,
    touch nothing, and carry a version.

FORMULA_VERSION IS A CONTRACT
    Every row in intraday_observations records the version that produced it. Bump it whenever an
    output would change for the same inputs — a corrected mapping, a changed convention, a new
    denominator. Then the rows computed under the old version can be FOUND
    (`WHERE formula_version = N`) and recomputed. Do NOT bump it for a comment or a refactor: a
    version change should always mean "these rows and those rows are not comparable".
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# v1 — 2026-08-26, issue #133. Initial: P/E trailing and forward from the in-effect statement row's
# EPS, FCF yield from free cash flow over live market cap.
FORMULA_VERSION = 1


def _num(value) -> Decimal | None:
    """A finite Decimal, or None. Never raises, never returns NaN or infinity.

    psycopg hands back Decimal, FMP hands back float or str, and a missing field is None. All three
    reach this function, and a NaN sneaking through would poison every comparison it touched
    silently — NaN is not equal to itself, so a bad row would not even show up as an outlier.
    """
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def pe(price, eps) -> Decimal | None:
    """Price / earnings per share.

    NEGATIVE VALUES ARE RETURNED, not nulled. A loss-making company has a negative P/E, and that is
    information — screening convention often renders it "N/A", but this table's job is to record
    what was true, not to decide how a page should show it. A consumer that wants to hide negatives
    can; a consumer reading a NULL cannot tell "loss-making" from "we had no EPS".

    Zero EPS returns None: the ratio is undefined, not infinite.
    """
    p, e = _num(price), _num(eps)
    if p is None or e is None or e == 0:
        return None
    return p / e


def fcf_yield(free_cash_flow, market_cap) -> Decimal | None:
    """Free cash flow / market capitalisation.

    Negative free cash flow yields a negative number, for the same reason as `pe` above — QBTS and
    SVRA in the current book both burn cash, and a NULL there would hide it.
    """
    fcf, cap = _num(free_cash_flow), _num(market_cap)
    if fcf is None or cap is None or cap <= 0:
        return None
    return fcf / cap


def compute(*, price, market_cap, fundamentals: dict | None) -> dict:
    """Every price-derived ratio for one observation.

    `fundamentals` is the ONE statement row in effect — never a forward-filled composite across
    several. When it is None, or when it lacks the figure a ratio needs, that ratio is None. The
    migration's ck_intraday_obs_ratios_have_lineage enforces the first half of that from the
    database side; this function is the second half.
    """
    if not fundamentals:
        return {"pe_trailing": None, "pe_forward": None, "fcf_yield": None}
    return {
        "pe_trailing": pe(price, fundamentals.get("eps_current")),
        "pe_forward": pe(price, fundamentals.get("eps_next_year_est")),
        "fcf_yield": fcf_yield(fundamentals.get("free_cash_flow"), market_cap),
    }
