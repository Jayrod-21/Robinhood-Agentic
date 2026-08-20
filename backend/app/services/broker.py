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
import threading
import time

from app.services import accounts
from app.services.snapshot import AccountSnapshot, SnapshotError, load_snapshot

logger = logging.getLogger("agentic.services.broker")

# A short cache in front of the broker. /api/account is polled every 10s per open tab per operator,
# and each poll is two Alpaca calls (account + positions). The number is small because the whole
# point is freshness; it exists to stop N tabs multiplying into N x 2 calls every ten seconds.
_CACHE_TTL_SECONDS = 5.0

# KEYED BY ACCOUNT. This was a single module-level tuple with no account dimension — the biggest
# single-account assumption in the codebase. With more than one account configured, an unkeyed
# cache serves whichever account was fetched most recently under whatever name the page asked for:
# every number real, every number belonging to someone else. That is the worst failure this
# dashboard could have, because nothing about it looks wrong.
_cache: dict[int, tuple[AccountSnapshot, float]] = {}
_lock = threading.Lock()


def alpaca_configured(account_id: int | None = None) -> bool:
    """True when the requested account has both halves of its credential.

    Checked as a pair on purpose: half a credential is a misconfiguration, not a source. Answering
    'configured' on the key alone would send the request and fail at 401, which reads as 'Alpaca is
    broken' rather than 'the secret is missing from backend/.env'.
    """
    return accounts.get_profile(account_id) is not None


def _fetch_alpaca(profile: accounts.AccountProfile) -> AccountSnapshot:
    from src.alpaca import AlpacaClient, AlpacaError, fetch_snapshot

    try:
        client = AlpacaClient(
            key_id=profile.key_id, secret=profile.secret_key, base_url=profile.base_url
        )
        payload = fetch_snapshot(client)
    except AlpacaError as exc:
        # str(exc) is safe to surface: src/alpaca.py redacts the credential from every message it
        # raises, and the text names the likely cause (wrong environment, missing key).
        logger.error("alpaca snapshot fetch failed: %s", exc)
        raise SnapshotError(
            f"The broker account could not be read: {exc}. The dashboard will not show stale "
            f"holdings from a different account instead — fix the connection and retry."
        ) from None
    return AccountSnapshot.model_validate(payload)


def get_snapshot(snapshot_path, account_id: int | None = None) -> AccountSnapshot:
    """The account, from the broker when configured, otherwise from the file.

    ``account_id`` selects among the configured profiles; omitted means the default account, which
    is what every existing caller gets. The FILE fallback is only ever the default account's — it is
    written by bin/alpaca_sync.sh from one account, so serving it for account 3 would answer with
    account 1's holdings under account 3's name.
    """
    profile = accounts.get_profile(account_id)
    if profile is None:
        if account_id not in (None, accounts.DEFAULT_ACCOUNT_ID):
            # Falling back to the file here would be that exact substitution, so refuse instead.
            raise SnapshotError(
                f"Account {account_id} is not configured on this deployment."
            )
        return load_snapshot(snapshot_path)

    now = time.monotonic()
    with _lock:
        cached = _cache.get(profile.id)
        if cached is not None and (now - cached[1]) < _CACHE_TTL_SECONDS:
            return cached[0]

    snapshot = _fetch_alpaca(profile)
    with _lock:
        _cache[profile.id] = (snapshot, time.monotonic())
    return snapshot


def reset_cache() -> None:
    """Drop every cached snapshot. TEST SUPPORT ONLY — nothing in the app calls this.

    The docstring used to claim "and the refresh path", which was never true even while that path
    existed: it rewrote the fallback FILE rather than anything this cache holds. The bridge is gone
    now, but the lesson outlived it — a docstring naming a caller that does not exist sends the next
    reader hunting for wiring that was never built.

    Nothing needs it, either — the TTL is five seconds, so a stale entry outlives its usefulness
    before anyone could act on it.
    """
    with _lock:
        _cache.clear()
