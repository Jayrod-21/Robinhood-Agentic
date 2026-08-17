"""Where the account snapshot comes from — the broker, or the file.

ONE CONTRACT, TWO SOURCES
    ``AccountSnapshot`` (services/snapshot.py) already described an account: masked number, cash,
    equity, positions with cost basis. It was written for the Robinhood file, but nothing in it is
    Robinhood-specific, so it serves as the broker interface unchanged. Alpaca is mapped into that
    same shape in src/alpaca.py; this module decides which source answers.

    Deliberately NOT a new abstraction layer. Inventing a second shape to sit "above" the one that
    already existed would fork every consumer — the dashboard, the marks overlay, the P&L math —
    for no gain.

SELECTION, AND WHY IT FAILS THE WAY IT DOES
    Alpaca is preferred whenever credentials are configured. When it is configured but unreachable,
    this REFUSES rather than falling back to the file. That is the important decision here.

    A fallback would be worse than an error. The file is a Robinhood snapshot from a different
    broker, months out of date, and silently serving it when Alpaca is down would show holdings the
    operator does not have, under a heading that says nothing is wrong. An outage is recoverable;
    a dashboard confidently displaying the wrong account is how someone acts on a position that
    isn't there. The failure is loud, names the source, and says what to check.

FRESHNESS
    A live broker read is genuinely current, which the file never was — but the snapshot still
    carries ``generated_at``, and the UI should keep showing it. "Live" is a property of a
    particular fetch, not of a data source, and a cached value five minutes old is exactly as stale
    whether it came from a file or an API.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from app.services.snapshot import AccountSnapshot, SnapshotError, load_snapshot

logger = logging.getLogger("agentic.services.broker")

# A short cache in front of the broker. /api/account is polled every 10s per open tab per operator,
# and each poll is two Alpaca calls (account + positions). The number is small because the whole
# point is freshness; it exists to stop N tabs multiplying into N x 2 calls every ten seconds.
_CACHE_TTL_SECONDS = 5.0

_cache: tuple[AccountSnapshot, float] | None = None
_lock = threading.Lock()


def alpaca_configured() -> bool:
    """True when both halves of the Alpaca credential are present.

    Checked as a pair on purpose: half a credential is a misconfiguration, not a source. Answering
    'configured' on the key alone would send the request and fail at 401, which reads as 'Alpaca is
    broken' rather than 'the secret is missing from backend/.env'.
    """
    return bool(
        (os.environ.get("ALPACA_API_KEY_ID") or "").strip()
        and (os.environ.get("ALPACA_API_SECRET_KEY") or "").strip()
    )


def _fetch_alpaca() -> AccountSnapshot:
    from src.alpaca import AlpacaError, fetch_snapshot

    try:
        payload = fetch_snapshot()
    except AlpacaError as exc:
        # str(exc) is safe to surface: src/alpaca.py redacts the credential from every message it
        # raises, and the text names the likely cause (wrong environment, missing key).
        logger.error("alpaca snapshot fetch failed: %s", exc)
        raise SnapshotError(
            f"The broker account could not be read: {exc}. The dashboard will not show stale "
            f"holdings from a different account instead — fix the connection and retry."
        ) from None
    return AccountSnapshot.model_validate(payload)


def get_snapshot(snapshot_path) -> AccountSnapshot:
    """The account, from the broker when configured, otherwise from the file."""
    if not alpaca_configured():
        return load_snapshot(snapshot_path)

    global _cache
    now = time.monotonic()
    with _lock:
        if _cache is not None and (now - _cache[1]) < _CACHE_TTL_SECONDS:
            return _cache[0]

    snapshot = _fetch_alpaca()
    with _lock:
        _cache = (snapshot, time.monotonic())
    return snapshot


def reset_cache() -> None:
    """Drop the cached snapshot. Test-support and the refresh path — never a request path."""
    global _cache
    with _lock:
        _cache = None
