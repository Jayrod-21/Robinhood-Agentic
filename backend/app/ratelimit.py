"""Shared process-local cooldown limiter for the paid (token-spending) streaming endpoints.

Both the debate router and the pipeline router expose independent endpoints that each call
``run_debate`` — and each debate spends real Anthropic tokens (a full jury plus synthesis). If the
two endpoints kept separate cooldown clocks the combined spend would be double the configured cap, so
they MUST share one budget. This module is that single budget: one named gate, guarded by a lock so
the read-modify-write of the timestamp is atomic across FastAPI's threadpool (sync endpoints) and the
event loop.

Threat model: this is the abuse guard on a billable path. A held button, a runaway client loop, or
two concurrent requests must not be able to fan out unbounded paid debates. The lock closes the
TOCTOU window where two callers both observe a stale "last run" timestamp and both pass the gate.
"""

from __future__ import annotations

import threading
import time


class CooldownLimiter:
    """A minimum-interval gate shared by every caller of the same instance.

    ``check_and_consume`` is the only mutating entry point: it atomically tests whether the interval
    has elapsed and, if so, stamps "now" as the last honored time. Returns the remaining wait seconds
    (>0) when the caller is inside the cooldown window, or 0 when the call is admitted.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_monotonic: float | None = None

    def check_and_consume(self, min_interval_seconds: float) -> int:
        """Return 0 if admitted (and record the time), else the ceil-ish seconds left to wait.

        A non-positive ``min_interval_seconds`` disables the gate (always admits).
        """
        if min_interval_seconds <= 0:
            return 0
        now = time.monotonic()
        with self._lock:
            if self._last_monotonic is not None:
                elapsed = now - self._last_monotonic
                if elapsed < min_interval_seconds:
                    # +1 so a sub-second remainder still tells the user to wait at least 1s.
                    return int(min_interval_seconds - elapsed) + 1
            self._last_monotonic = now
            return 0

    def reset(self) -> None:
        """Clear the gate. Test-support only — never called from request paths."""
        with self._lock:
            self._last_monotonic = None


# One shared budget for every token-spending debate path (debate router + pipeline router).
debate_limiter = CooldownLimiter()

# Separate gate for the scan stream. A scan spends no Anthropic tokens but fans out one blocking
# yfinance fetch per ticker (up to the whole universe with an empty body), so an unthrottled client
# loop is a path to getting the host IP banned by Yahoo. It is deliberately NOT the debate limiter:
# a free scan must never consume the paid-debate budget, and vice versa.
scan_limiter = CooldownLimiter()
