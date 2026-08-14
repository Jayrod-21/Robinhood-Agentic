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
from collections import OrderedDict, deque


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


class KeyedWindowLimiter:
    """One :class:`WindowLimiter` budget per key, with a bounded number of keys.

    WHY THIS EXISTS
        The auth gate was a single unkeyed budget. That is the correct shape for bounding total
        Argon2 CPU, and the WRONG shape for deciding who gets refused: with one budget shared by
        every caller, anyone on the internet can spend it and deny sign-in to the operators. That
        was survivable only while Caddy basic-auth kept strangers away from the login form. When
        the outer gate was removed (AUTH_THREAT_MODEL §5.13), the shared budget became a
        one-request-per-five-seconds denial-of-service against the people who own the account.

        So the auth gate is now two gates: this one, per client, which decides WHO is refused, and
        the unkeyed one, which caps TOTAL work regardless of how many clients show up. An attacker
        spending their own per-key budget cannot touch anyone else's; an attacker spreading across
        many keys still hits the global ceiling.

    MEMORY IS BOUNDED, DELIBERATELY
        A per-key dict fed by attacker-chosen keys is itself a memory-exhaustion vector — the exact
        bug class the auth gate exists to prevent. ``max_keys`` caps the dict; when it is full the
        least-recently-admitted key is evicted. Eviction is safe: a key's absence means "no
        recorded grants", i.e. the newcomer starts with a full budget, which is what a first-time
        caller gets anyway. The global ceiling is what holds the line while keys churn, which is
        why this class must never be used alone on a credential path.
    """

    def __init__(self, max_keys: int = 4096) -> None:
        self._lock = threading.Lock()
        self._max_keys = max_keys
        # insertion-ordered: the first key is the least-recently-admitted, so eviction is popitem
        # on the front. Re-inserted on each admit to keep the ordering meaningful.
        self._buckets: OrderedDict[str, deque[float]] = OrderedDict()

    def check_and_consume(self, key: str, max_requests: int, window_seconds: float) -> int:
        """Return 0 if admitted for ``key``, else seconds until that key's oldest grant ages out.

        Same convention as the other limiters: a non-positive budget or window disables the gate.
        """
        if max_requests <= 0 or window_seconds <= 0:
            return 0
        now = time.monotonic()
        with self._lock:
            admitted = self._buckets.get(key)
            if admitted is None:
                admitted = deque()
            cutoff = now - window_seconds
            while admitted and admitted[0] <= cutoff:
                admitted.popleft()
            if len(admitted) >= max_requests:
                # Keep the bucket (and its position) so a blocked caller cannot evict itself into
                # a fresh budget by hammering — the refusal must be sticky for the full window.
                self._buckets[key] = admitted
                return int(admitted[0] + window_seconds - now) + 1
            admitted.append(now)
            self._buckets[key] = admitted
            self._buckets.move_to_end(key)
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
            return 0

    def reset(self) -> None:
        """Clear every bucket. Test-support only — never called from request paths."""
        with self._lock:
            self._buckets.clear()


# One shared budget for every token-spending debate path (debate router + pipeline router).
debate_limiter = CooldownLimiter()

# Separate gate for the scan stream. A scan spends no Anthropic tokens but fans out one blocking
# FMP bundle per ticker (up to the whole universe with an empty body), so an unthrottled client
# loop is a path to getting the host IP banned by Yahoo. It is deliberately NOT the debate limiter:
# a free scan must never consume the paid-debate budget, and vice versa.
scan_limiter = CooldownLimiter()

# NOTE: there is deliberately no module-global auth limiter here. The §5.1 route-wide auth gate is
# a WindowLimiter created per application instance (see app.routers.auth.enforce_auth_cooldown):
# only one router draws from it, so nothing needs the cross-module sharing that forces
# debate_limiter to be a global — and a global would couple every app instance in the process
# (every TestClient app in a pytest run) to one budget. Production runs exactly one app per
# process, so per-app IS the single in-process budget §3.3 calls for.
