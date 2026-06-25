"""Live market marks via yfinance, with a short TTL cache and per-symbol soft-fail.

yfinance is unofficial and rate-limited, so this module is defensive by construction: a failure to
price one symbol returns ``None`` for that symbol and never raises, and results are cached for a few
seconds so a dashboard that polls every couple of seconds doesn't hammer the upstream. Network I/O
is synchronous here; callers run it off the event loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("agentic.marks")

# symbol -> (price_or_None, monotonic_timestamp)
_CACHE: dict[str, tuple[float | None, float]] = {}
_LOCK = threading.Lock()


def _fetch_one(symbol: str) -> float | None:
    """Best-effort last price for one symbol. Returns None on any failure."""
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        # fast_info is the cheap path; fall back to .info's currentPrice if it's empty.
        price = None
        try:
            price = ticker.fast_info.get("last_price")
        except Exception:  # noqa: BLE001 — fast_info can raise on odd symbols
            price = None
        if price is None:
            price = ticker.info.get("currentPrice")
        if price is None:
            return None
        price = float(price)
        return price if price == price and price > 0 else None  # reject NaN / non-positive
    except Exception as exc:  # noqa: BLE001 — pricing one name must never crash the request
        logger.warning("mark fetch failed for %s: %s", symbol, exc)
        return None


def get_marks(symbols: list[str], ttl_seconds: int) -> dict[str, float | None]:
    """Return {symbol: last_price | None}, served from cache within ``ttl_seconds``."""
    now = time.monotonic()
    out: dict[str, float | None] = {}
    to_fetch: list[str] = []

    with _LOCK:
        for sym in symbols:
            cached = _CACHE.get(sym)
            if cached is not None and (now - cached[1]) < ttl_seconds:
                out[sym] = cached[0]
            else:
                to_fetch.append(sym)

    for sym in to_fetch:
        price = _fetch_one(sym)
        with _LOCK:
            _CACHE[sym] = (price, time.monotonic())
        out[sym] = price

    return out
