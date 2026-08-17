"""Live market marks via FMP, with a short TTL cache and per-symbol soft-fail.

Defensive by construction: a failure to price one symbol returns ``None`` for that symbol and never
raises, and results are cached so a dashboard polling every ten seconds does not fetch per tab.
Network I/O is synchronous here; callers run it off the event loop via ``asyncio.to_thread``.

WHY THE CACHE MATTERS MORE THAN IT LOOKS
    FMP's Starter plan has no batch-quote endpoint (`batch-quote` answers 402), so N positions cost
    N calls. The dashboard polls /api/account every 10s, per open tab, per operator. Without the
    cache that is 8 positions x 2 operators x however many tabs, every ten seconds, against a
    300/min ceiling. The cache is process-wide and keyed by symbol, so every caller in the process
    shares one fetch — and the client itself is the shared singleton, so the rate gate sees all of
    it (src/fmp.py::get_shared_client).

    Replaced yfinance, which was an unofficial scrape that rate-limited by IP. FMP is the paid feed
    the rest of the data path now uses; one source, one set of failure modes.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("agentic.marks")

# The provider these marks come from, declared HERE — beside _fetch_one, the function that actually
# calls it — so anything displaying the source reads it from the code that does the work. A literal
# "FMP" in a route, or a config field, is true until the day the provider changes and then it is a
# lie on a page whose entire job is telling the operator what to trust. /api/health reported the
# wrong broker for exactly this reason.
MARKS_PROVIDER = "FMP"

# symbol -> (price_or_None, monotonic_timestamp)
_CACHE: dict[str, tuple[float | None, float]] = {}
_LOCK = threading.Lock()


def _fetch_one(symbol: str) -> float | None:
    """Best-effort last price for one symbol. Returns None on any failure."""
    try:
        from src.fmp import quote

        row = quote(symbol)
        if not row:
            return None
        price = row.get("price")
        if price is None:
            return None
        price = float(price)
        # Reject NaN and non-positive: a zero mark would price a position at nothing and render as
        # a total loss on the dashboard, which is a far worse answer than "unavailable".
        return price if price == price and price > 0 else None
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
