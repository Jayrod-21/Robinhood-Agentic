"""Process-local, lock-guarded rate-limit gates for abuse-priced endpoints.

Two shapes live here:

* :class:`CooldownLimiter` — a minimum-interval gate for the paid (token-spending) streaming
  endpoints. Both the debate router and the pipeline router expose independent endpoints that each
  call ``run_debate`` — and each debate spends real Anthropic tokens (a full jury plus synthesis).
  If the two endpoints kept separate cooldown clocks the combined spend would be double the
  configured cap, so they MUST share one budget: one named gate, guarded by a lock so the
  read-modify-write of the timestamp is atomic across FastAPI's threadpool (sync endpoints) and
  the event loop.
* :class:`WindowLimiter` — a rolling-window budget for the auth routes (AUTH_THREAT_MODEL
  §5.1/§3.3), where the legitimate flow is itself a burst (password POST, then TOTP POST seconds
  later) that a minimum-interval gate would refuse.

Threat model: these are the abuse guards on billable / credential-processing paths. A held button,
a runaway client loop, or two concurrent requests must not be able to fan out unbounded paid
debates — or unbounded 64 MiB Argon2 verifications. The lock closes the TOCTOU window where two
callers both observe stale state and both pass the gate.
"""

from __future__ import annotations

import threading
import time
from collections import deque


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


class WindowLimiter:
    """A rolling-window budget: admits up to ``max_requests`` per ``window_seconds``.

    Same contract and locking discipline as :class:`CooldownLimiter` — ``check_and_consume`` is
    the only mutating entry point, atomic under the lock, returning 0 on admit or the seconds left
    until a slot frees. The difference is shape: a minimum-interval gate cannot admit the
    legitimate multi-request auth flow (password POST, then TOTP POST seconds later, §4) while
    still capping the total, so the auth routes need a budget over a window rather than a spacing
    rule.

    Only ADMITTED requests consume the budget: a caller hammering while blocked does not push its
    own wait further out — the mirror of CooldownLimiter, where a refused call never re-stamps the
    clock. Memory is bounded at ``max_requests`` timestamps.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._admitted: deque[float] = deque()

    def check_and_consume(self, max_requests: int, window_seconds: float) -> int:
        """Return 0 if admitted (recording the grant), else seconds until the oldest grant ages out.

        A non-positive ``max_requests`` or ``window_seconds`` disables the gate (always admits) —
        the same convention as :meth:`CooldownLimiter.check_and_consume`.
        """
        if max_requests <= 0 or window_seconds <= 0:
            return 0
        now = time.monotonic()
        with self._lock:
            cutoff = now - window_seconds
            while self._admitted and self._admitted[0] <= cutoff:
                self._admitted.popleft()
            if len(self._admitted) >= max_requests:
                # +1 so a sub-second remainder still tells the caller to wait at least 1s.
                return int(self._admitted[0] + window_seconds - now) + 1
            self._admitted.append(now)
            return 0

    def reset(self) -> None:
        """Clear the gate. Test-support only — never called from request paths."""
        with self._lock:
            self._admitted.clear()


# One shared budget for every token-spending debate path (debate router + pipeline router).
debate_limiter = CooldownLimiter()

# Separate gate for the scan stream. A scan spends no Anthropic tokens but fans out one blocking
# yfinance fetch per ticker (up to the whole universe with an empty body), so an unthrottled client
# loop is a path to getting the host IP banned by Yahoo. It is deliberately NOT the debate limiter:
# a free scan must never consume the paid-debate budget, and vice versa.
scan_limiter = CooldownLimiter()

# NOTE: there is deliberately no module-global auth limiter here. The §5.1 route-wide auth gate is
# a WindowLimiter created per application instance (see app.routers.auth.enforce_auth_cooldown):
# only one router draws from it, so nothing needs the cross-module sharing that forces
# debate_limiter to be a global — and a global would couple every app instance in the process
# (every TestClient app in a pytest run) to one budget. Production runs exactly one app per
# process, so per-app IS the single in-process budget §3.3 calls for.
