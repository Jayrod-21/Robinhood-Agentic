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
