"""Fundamentals adapter — maps a provider's fields into the screen's ``fundamentals`` dict.

The provider is FMP (src/fmp.py). Every fetch is wrapped so a single bad or missing ticker degrades
gracefully (returns ``None``) instead of taking down the whole scan. Network access is isolated
here so ``screen.py`` stays pure and testable.

The yfinance adapter this replaced was deleted rather than left in place. It was unreachable once
the callers moved, and an unreachable mapping for a retired provider is worse than no mapping: it
still looks like a supported path, and the next person to need a second source would copy it.
Corporate actions and delistings still use yfinance in their own loader images (bin/db_corporate_
actions.sh) — those are a separate migration, and they are the last of it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("agentic.data")


def _safe_num(value) -> float | None:
    """Coerce to float, treating NaN / None / non-numeric as missing."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # yfinance uses NaN for missing numeric fields.
    if f != f:  # NaN check without importing math
        return None
    return f


# --- FMP adapter ------------------------------------------------------------------------------
# The replacement for the yfinance path above. Kept in the same module so the two mappings sit
# side by side and the screen's expected shape is defined in exactly one place.


def _fraction_to_percent(value) -> float | None:
    """FMP margins/growth arrive as fractions (0.469). The screen's percent-valued fields want
    46.9. Explicit rather than inline so a unit mistake is one named function, not a stray *100."""
    f = _safe_num(value)
    return None if f is None else f * 100.0


def fundamentals_from_fmp(bundle: dict) -> dict | None:
    """Translate an :meth:`src.fmp.FmpClient.fundamentals_bundle` into the screen's shape.

    Pure (no network), so it is unit-tested against captured FMP payloads.

    UNITS — the part that silently corrupts gates if wrong. Verified against a real AAPL payload
    on 2026-08-14, not assumed from documentation:
      * ``grossProfitMargin`` 0.4690 and ``revenueGrowth`` 0.0643 are FRACTIONS, which is what
        yfinance's ``grossMargins``/``revenueGrowth`` also were — so those two pass through
        unchanged and the existing gate thresholds keep their meaning.
      * ``fcf_yield`` is a PERCENT in this codebase (3.0 == 3%, per the screen spec), and FMP does
        not supply it. It is derived here exactly as the yfinance path derived it.
    Returns None when market cap is missing, matching fetch_fundamentals' contract: no market cap
    means the tiered gates cannot run, and a row that cannot be gated must not look like a failure
    to pass them.
    """
    profile = bundle.get("profile") or {}
    ratios = bundle.get("ratios") or {}
    income = bundle.get("income") or {}
    cash_flow = bundle.get("cash_flow") or {}
    growth = bundle.get("growth") or {}

    market_cap = _safe_num(profile.get("marketCap"))
    if market_cap is None:
        return None

    free_cash_flow = _safe_num(cash_flow.get("freeCashFlow"))
    fcf_yield = None
    if free_cash_flow is not None and market_cap:
        fcf_yield = free_cash_flow / market_cap * 100.0

    return {
        "ticker": profile.get("symbol"),
        "market_cap": market_cap,
        # FMP names it priceToEarningsGrowthRatio; the legacy v3 field was pegRatio.
        "peg": _safe_num(ratios.get("priceToEarningsGrowthRatio")),
        "fcf_yield": fcf_yield,
        "free_cash_flow": free_cash_flow,
        "net_income": _safe_num(income.get("netIncome")),
        "operating_cash_flow": _safe_num(cash_flow.get("netCashProvidedByOperatingActivities")),
        "gross_margin": _safe_num(ratios.get("grossProfitMargin")),
        "revenue_growth": _safe_num(growth.get("revenueGrowth")),
        # Carried for the agent's Wasden lens, not used by the numeric gates:
        "name": profile.get("companyName"),
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "trailing_pe": _safe_num(ratios.get("priceToEarningsRatio")),
        "forward_pe": _safe_num(ratios.get("forwardPriceToEarningsGrowthRatio")),
        "price": _safe_num(profile.get("price")),
    }


def known_at_from_statement(row: dict) -> str | None:
    """When a statement actually became public — for ``fundamentals_snapshots.known_at``.

    THE LOOK-AHEAD TRAP. FMP's ``date`` is the PERIOD END; ``acceptedDate`` is when the filing was
    accepted. For AAPL's FY2025 those are 2025-09-27 and 2025-10-31 — five weeks apart. Stamping
    known_at with the period end would let a backtest read figures that were not public for another
    month, which is the single most effective way to make a losing strategy look profitable.
    Prefers acceptedDate, falls back to filingDate, and returns None rather than guessing — the
    column is nullable and the point-in-time index excludes NULLs, so an undated row is invisible
    to point-in-time queries instead of silently wrong in them.
    """
    for field_name in ("acceptedDate", "filingDate"):
        value = (row or {}).get(field_name)
        if value:
            stamped = _as_utc(str(value))
            if stamped:
                return stamped
    return None


# SEC filings are accepted on EASTERN time, and FMP reports acceptedDate that way — naive, no zone.
# Storing it naive let Postgres read it in the session zone and shift it: SVRA's FY2023 acceptance
# landed as 2023-12-30 19:00 UTC for a period ending 2023-12-31, tripping the known_at >= period_end
# CHECK. The constraint caught it, but the real damage was quieter — a timestamp shifted five hours
# EARLY says a filing was knowable before it was, which is the exact error `known_at` exists to stop.
_FILING_TZ = "America/New_York"


def _as_utc(raw: str) -> str | None:
    """Interpret a naive filing timestamp as Eastern and return it as explicit UTC."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    text = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        # A date with no clock is the END of that day in Eastern: a filing accepted "on the 31st"
        # was not knowable at 00:00. Rounding it to midnight would be the same early-by-hours error
        # in a smaller form.
        if fmt == "%Y-%m-%d":
            naive = naive.replace(hour=23, minute=59, second=59)
        return (
            naive.replace(tzinfo=ZoneInfo(_FILING_TZ))
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    logger.warning("unparseable filing timestamp %r — leaving it undated", raw)
    return None


def fetch_fundamentals_fmp(ticker: str) -> dict | None:
    """Live fundamentals for one ticker from FMP. The replacement for fetch_fundamentals.

    Returns None on any failure — same contract as the yfinance version it replaces, because the
    screen's callers already treat None as "cannot gate this name" rather than "this name failed".

    Costs 5 calls (one bundle). Pacing and the shared rate gate live in src.fmp; nothing here
    retries, because the client already does.
    """
    try:
        from src.fmp import get_shared_client

        bundle = get_shared_client().fundamentals_bundle(ticker)
        return fundamentals_from_fmp(bundle)
    except Exception as exc:  # noqa: BLE001 — one bad ticker must never take down a scan
        logger.warning("fetch_fundamentals_fmp(%s) failed: %s", ticker, exc)
        return None


# --- the wider fundamental set ----------------------------------------------------------------
# Everything the owner's Bloomberg pull carries, sourced from FMP where it exists and computed
# where it does not. See db/migrations/016_fundamentals_full.up.sql for why derived values are
# marked rather than mixed in silently.


def _split_range(raw) -> tuple[float | None, float | None]:
    """FMP's profile carries the 52-week range as one string, "169.21-260.10"."""
    if not isinstance(raw, str) or "-" not in raw:
        return None, None
    lo, _, hi = raw.partition("-")
    return _safe_num(lo), _safe_num(hi)


def wide_fundamentals_from_fmp(bundle: dict) -> tuple[dict, dict]:
    """Map a bundle into the full fundamentals row, plus a record of what we DERIVED.

    Returns ``(row, derived)``. ``derived`` names each computed key and the formula behind it, so a
    figure that looks wrong can be traced to either the vendor or our arithmetic — they are
    indistinguishable once both sit in adjacent columns.

    Absent inputs yield None, never zero or a guess. A zero P/B reads as "this company trades at
    nothing", which is a claim; None reads as "we do not know", which is the truth.
    """
    profile = bundle.get("profile") or {}
    ratios = bundle.get("ratios") or {}
    income = bundle.get("income") or {}
    cash = bundle.get("cash_flow") or {}
    balance = bundle.get("balance") or {}
    metrics = bundle.get("key_metrics") or {}
    target = bundle.get("price_target") or {}
    grades = bundle.get("grades") or {}
    derived: dict[str, str] = {}

    revenue = _safe_num(income.get("revenue"))
    rd = _safe_num(income.get("researchAndDevelopmentExpenses"))
    equity = _safe_num(balance.get("totalStockholdersEquity"))
    assets = _safe_num(balance.get("totalAssets"))
    price = _safe_num(profile.get("price"))
    tbvps = _safe_num(ratios.get("tangibleBookValuePerShare"))
    lo52, hi52 = _split_range(profile.get("range"))

    rd_to_revenue = None
    if rd is not None and revenue:
        rd_to_revenue = rd / revenue
        derived["rd_to_revenue"] = "researchAndDevelopmentExpenses / revenue"

    equity_to_assets = None
    if equity is not None and assets:
        equity_to_assets = equity / assets
        derived["equity_to_assets"] = "totalStockholdersEquity / totalAssets"

    price_to_tangible_book = None
    if price is not None and tbvps:
        price_to_tangible_book = price / tbvps
        derived["price_to_tangible_book"] = "profile.price / ratios.tangibleBookValuePerShare"

    # EBITDA / interest expense, computed rather than taken from FMP's interestCoverageRatio.
    # Two reasons, both the same defect class:
    #   * FMP's ratio is EBIT-based, so storing it under a column named `ebitda_interest` would put
    #     one number behind another number's name.
    #   * FMP returns 0.0 when it has no interest expense to divide by (AAPL FY2025 arrives as
    #     exactly 0). Rendered as "0.00x" that reads as "cannot cover its interest at all" — the
    #     precise inversion of the truth for a company whose interest burden is negligible.
    # A company with no interest expense has UNDEFINED coverage, not zero. That is None here, and
    # the page shows it as unknown.
    ebitda = _safe_num(income.get("ebitda"))
    interest = _safe_num(income.get("interestExpense"))
    ebitda_interest = None
    if ebitda is not None and interest:
        ebitda_interest = ebitda / abs(interest)
        derived["ebitda_interest"] = "ebitda / abs(interestExpense)"

    if lo52 is not None or hi52 is not None:
        derived["week_52_high"] = "split from profile.range"
        derived["week_52_low"] = "split from profile.range"

    row = {
        "dividend_yield": _safe_num(ratios.get("dividendYield")),
        "ev_to_ebitda": _safe_num(ratios.get("enterpriseValueMultiple")),
        "price_to_book": _safe_num(ratios.get("priceToBookRatio")),
        "price_to_sales": _safe_num(ratios.get("priceToSalesRatio")),
        "price_to_tangible_book": price_to_tangible_book,
        "beta": _safe_num(profile.get("beta")),
        "week_52_high": hi52,
        "week_52_low": lo52,
        "avg_volume_30d": int(v) if (v := _safe_num(profile.get("averageVolume"))) else None,
        "revenue_ttm": revenue,
        "ebitda_ttm": ebitda,
        "capital_expenditure": _safe_num(cash.get("capitalExpenditure")),
        "net_debt": _safe_num(balance.get("netDebt")),
        "shares_outstanding": _safe_num(income.get("weightedAverageShsOut")),
        "tangible_book_value_per_share": tbvps,
        "rd_to_revenue": rd_to_revenue,
        "equity_to_assets": equity_to_assets,
        "roe": _safe_num(metrics.get("returnOnEquity")),
        "roc": _safe_num(metrics.get("returnOnInvestedCapital")),
        "debt_to_equity": _safe_num(ratios.get("debtToEquityRatio")),
        "ebitda_interest": ebitda_interest,
        "cash_conversion_cycle": _safe_num(metrics.get("cashConversionCycle")),
        "eps_current": _safe_num(income.get("eps")),
        "analyst_target_price": _safe_num(target.get("targetConsensus")),
        "analyst_recommendation": grades.get("consensus"),
        # Deliberately absent, and recorded as such rather than left to be discovered:
        #   short_interest       — no FMP endpoint on this plan
        #   eps_next_year_est    — the analyst-estimates endpoint answers 400 here
        #   insider/institutional ownership — the ownership endpoint 404s on this plan
        "short_interest": None,
        "eps_next_year_est": None,
    }
    return row, derived


def piotroski_inputs(period_income: dict, period_cash: dict, period_balance: dict) -> dict:
    """Reshape one annual period into the nine-signal input dict src/piotroski.py expects."""
    return {
        "period_end": (period_income or {}).get("date"),
        "net_income": (period_income or {}).get("netIncome"),
        "cfo": (period_cash or {}).get("netCashProvidedByOperatingActivities"),
        "shares_out": (period_income or {}).get("weightedAverageShsOut"),
        "total_assets": (period_balance or {}).get("totalAssets"),
        "long_term_debt": (period_balance or {}).get("longTermDebt"),
        "current_assets": (period_balance or {}).get("totalCurrentAssets"),
        "current_liabilities": (period_balance or {}).get("totalCurrentLiabilities"),
        "cost_of_revenue": (period_income or {}).get("costOfRevenue"),
        "revenue": (period_income or {}).get("revenue"),
    }


def eps_growth_yoy(periods_income: list[dict]) -> float | None:
    """EPS growth from two annual periods. Derived — FMP has no such field on this plan.

    None when the prior EPS is zero or negative: a growth rate off a negative base is a number
    with no interpretation, and reporting one would be worse than reporting nothing.
    """
    if not periods_income or len(periods_income) < 2:
        return None
    cur = _safe_num(periods_income[0].get("eps"))
    prior = _safe_num(periods_income[1].get("eps"))
    if cur is None or prior is None or prior <= 0:
        return None
    return (cur - prior) / prior
