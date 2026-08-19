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
from typing import NamedTuple

logger = logging.getLogger("agentic.marks")

# The provider these marks come from, declared HERE — beside _fetch_one, the function that actually
# calls it — so anything displaying the source reads it from the code that does the work. A literal
# "FMP" in a route, or a config field, is true until the day the provider changes and then it is a
# lie on a page whose entire job is telling the operator what to trust. /api/health reported the
# wrong broker for exactly this reason.
MARKS_PROVIDER = "FMP"

# symbol -> (price, monotonic_timestamp_of_that_price)
#
# ONLY GOOD PRICES LIVE HERE. The previous version wrote the fetch result unconditionally, so a
# failed fetch stored (None, now) — destroying the last known price. Once the daily FMP budget ran
# out, every position went unpriced and STAYED unpriced, because the cache had overwritten itself
# with nothing. The dashboard blanked about ten minutes after a restart and could not recover
# without one. A cache that discards good data on failure is worse than no cache at all.
_CACHE: dict[str, tuple[float, float]] = {}

# symbol -> monotonic timestamp of the last FAILED attempt. Kept apart from the price so a failure
# can throttle retries without touching what we know.
_FAILED_AT: dict[str, float] = {}
_LOCK = threading.Lock()

# How long to leave a symbol alone after a failed fetch. Without this, an exhausted budget means
# every request re-attempts every symbol — fifteen guaranteed failures and fifteen log lines per
# page poll, which buries the one line that says what is actually wrong.
_RETRY_BACKOFF_SECONDS = 60.0

# How long a stale price may still be served. Past this it is withheld: a mark from an hour ago is
# not a mark, and pricing a position on it would put a confident number on a stale fact.
_MAX_STALE_SECONDS = 30 * 60.0


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


class Mark(NamedTuple):
    """A price and how much to trust it.

    ``stale`` means the price is real but older than the TTL: a refresh was attempted and failed, so
    the last known value is being served rather than nothing. Callers must surface that — a stale
    price shown as current is exactly the class of lie this project exists to avoid — but showing
    nothing when a ten-minute-old price is available is its own failure, and the one that blanked
    the dashboard.
    """

    price: float | None
    stale: bool
    age_seconds: float | None


def get_marks_detailed(symbols: list[str], ttl_seconds: int) -> dict[str, Mark]:
    """Prices with their freshness. The full contract; ``get_marks`` is the plain-price view."""
    now = time.monotonic()
    out: dict[str, Mark] = {}
    to_fetch: list[str] = []

    with _LOCK:
        for sym in symbols:
            cached = _CACHE.get(sym)
            if cached is not None and (now - cached[1]) < ttl_seconds:
                out[sym] = Mark(cached[0], False, now - cached[1])
                continue
            # Past its TTL. Only re-attempt if we are not inside the backoff from a recent failure;
            # otherwise fall through to the stale-serving path below without spending a call.
            failed_at = _FAILED_AT.get(sym)
            if failed_at is not None and (now - failed_at) < _RETRY_BACKOFF_SECONDS:
                out[sym] = _stale_or_nothing(sym, now)
            else:
                to_fetch.append(sym)

    for sym in to_fetch:
        price = _fetch_one(sym)
        with _LOCK:
            if price is not None:
                _CACHE[sym] = (price, time.monotonic())
                _FAILED_AT.pop(sym, None)
                out[sym] = Mark(price, False, 0.0)
            else:
                # The fetch failed. Keep whatever we already knew — that is the whole fix.
                _FAILED_AT[sym] = time.monotonic()
                out[sym] = _stale_or_nothing(sym, time.monotonic())

    return out


def _stale_or_nothing(symbol: str, now: float) -> Mark:
    """The last known price if it is recent enough to still mean something, else nothing.

    Caller must already hold _LOCK, or be in a section where it does not matter.
    """
    cached = _CACHE.get(symbol)
    if cached is None:
        return Mark(None, False, None)
    age = now - cached[1]
    if age > _MAX_STALE_SECONDS:
        return Mark(None, False, age)
    return Mark(cached[0], True, age)


def resolve_ttl_seconds(fallback: int) -> int:
    """The operator's chosen refresh cadence, or ``fallback`` when settings are unreadable.

    Centralised because six routers price positions and each one resolving this itself is six
    chances for the dashboard and the reconciliation to disagree about how old a price may be.
    """
    try:
        from app.services import settings_store

        return int(settings_store.get("marks_ttl_seconds"))
    except Exception:  # noqa: BLE001 — a settings failure must never stop pricing
        return fallback


def get_marks(symbols: list[str], ttl_seconds: int) -> dict[str, float | None]:
    """Return {symbol: last_price | None}, served from cache within ``ttl_seconds``.

    Kept for the callers that only need a number. A stale price is returned as a price — the
    freshness lives in :func:`get_marks_detailed`, and any surface that shows the number to an
    operator should be reading that instead.
    """
    return {sym: mark.price for sym, mark in get_marks_detailed(symbols, ttl_seconds).items()}


def reset_cache() -> None:
    """TEST SUPPORT ONLY."""
    with _LOCK:
        _CACHE.clear()
        _FAILED_AT.clear()
