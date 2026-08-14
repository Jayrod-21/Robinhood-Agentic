"""Free fundamentals adapter — maps yfinance fields into the screen's ``fundamentals`` dict.

yfinance is unofficial and rate-limited; every fetch is wrapped so a single bad/missing ticker
degrades gracefully (returns ``None``) instead of taking down the whole scan. Network access is
isolated here so ``screen.py`` stays pure and testable.
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


def fundamentals_from_info(info: dict) -> dict:
    """Translate a yfinance ``.info`` dict into the screen's fundamentals shape.

    Pure (no network) so it can be unit-tested against captured ``.info`` payloads.
    """
    market_cap = _safe_num(info.get("marketCap"))
    free_cash_flow = _safe_num(info.get("freeCashflow"))

    fcf_yield = None
    if free_cash_flow is not None and market_cap:
        # Stored as a percentage to match the spec (3.0 == 3%).
        fcf_yield = free_cash_flow / market_cap * 100.0

    # PEG: prefer the trailing field yfinance now exposes, fall back to legacy.
    peg = _safe_num(info.get("trailingPegRatio"))
    if peg is None:
        peg = _safe_num(info.get("pegRatio"))

    return {
        "market_cap": market_cap,
        "peg": peg,
        "fcf_yield": fcf_yield,
        "free_cash_flow": free_cash_flow,
        "net_income": _safe_num(info.get("netIncomeToCommon")),
        "operating_cash_flow": _safe_num(info.get("operatingCashflow")),
        "gross_margin": _safe_num(info.get("grossMargins")),
        "revenue_growth": _safe_num(info.get("revenueGrowth")),
        # Carried for the agent's Wasden lens, not used by the numeric gates:
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "trailing_pe": _safe_num(info.get("trailingPE")),
        "forward_pe": _safe_num(info.get("forwardPE")),
        "price": _safe_num(info.get("currentPrice")),
    }


def fetch_fundamentals(ticker: str) -> dict | None:
    """Fetch and map fundamentals for one ticker via yfinance. Returns None on failure."""
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info
        if not info or _safe_num(info.get("marketCap")) is None:
            logger.warning("No usable info for %s", ticker)
            return None
        data = fundamentals_from_info(info)
        data["ticker"] = ticker
        return data
    except Exception as exc:  # noqa: BLE001 — network/parse failures must not crash the scan
        logger.warning("fetch_fundamentals(%s) failed: %s", ticker, exc)
        return None


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
            return str(value)
    return None
