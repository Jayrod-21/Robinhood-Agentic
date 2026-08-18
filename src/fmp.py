"""Financial Modeling Prep client — the fundamentals source that replaces yfinance.

WHY THIS EXISTS
    The Wasden screen gates on fundamentals (market cap, PEG, FCF yield, margins, growth), and
    those were being pulled from yfinance on every scan and then thrown away. yfinance is an
    unofficial scrape of a consumer endpoint: it rate-limits by IP, changes field names without
    notice, and returns a *current* snapshot with no notion of when a figure became public. That
    last one is disqualifying for anything that backtests — a screen that reads today's numbers
    against a past date is looking into the future.

    FMP is a licensed API with dated statements, so every row this module produces can be stamped
    with `known_at` and filtered point-in-time by `db.fundamentals_snapshots`'s §ix_fundamentals_pit
    index.

THE /stable/ SURFACE, NOT /api/v3/
    FMP retired /api/v3 for every account created after 2025-08-31; those paths now answer 403
    "Legacy Endpoint" regardless of plan. Verified against this project's key on 2026-08-14: all
    five endpoints below answer 200 on /stable/, and every /api/v3/ equivalent answers 403. Repo
    comments predating that change still say "FMP once purchased" and assume v3 — they are stale,
    not a second source of truth.

WHAT THE KEY REACHES (Starter Annual, verified by probe on 2026-08-14, not read off a pricing page)
      * profile, ratios, income-statement, cash-flow-statement, financial-growth  -> 200
      * company-screener                                                          -> 200
      * historical-price-eod/full, quarterly statements                           -> 200
    company-screener answered 402 before the upgrade and 200 after, on the SAME key. It matters
    because it changes the shape of a universe scan: one bulk call to filter, then per-symbol
    bundles only for the names that survive — not five calls for every ticker considered.

PACING (see DEFAULT_CALLS_PER_MINUTE)
    The plan meters 300 calls/MINUTE with no daily cap. Five endpoints per symbol means a
    200-symbol bundle pass is 1000 calls — comfortably allowed, but only if it is spread over
    ~4 minutes rather than fired at once. `MinuteRateGate` paces by waiting; `CallBudget` is an
    optional hard stop that defaults to OFF, because inventing a daily ceiling on a per-minute plan
    would refuse work that was paid for.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

BASE_URL = "https://financialmodelingprep.com/stable"
DEFAULT_TIMEOUT_S = 20.0
# THE BINDING CONSTRAINT IS PER-MINUTE, NOT PER-DAY.
#
# The Starter Annual plan meters 300 calls/minute with no daily call cap (bandwidth is capped
# separately at 20 GB/30 days, which per-symbol JSON will not approach). An earlier version of this
# module modelled a DAILY budget, defaulted to 240, and would have refused the 241st call of the
# day on a plan that allows 432,000 — throttling work the owner had paid for, while doing nothing
# about the limit that actually exists.
#
# So: pace against the per-minute ceiling, and treat a daily cap as an OPTIONAL cost control that
# defaults to off. 270 leaves a 10% margin for clock skew between this process's rolling window and
# FMP's, and for any ad-hoc call made outside this client.
DEFAULT_CALLS_PER_MINUTE = 270
PLAN_CALLS_PER_MINUTE = 300  # documented ceiling; the default above sits under it deliberately

# 0 means "no daily cap", which is correct for a metered-per-minute plan. This exists only so a
# runaway backfill can be given a hard stop on purpose.
DEFAULT_DAILY_CALL_BUDGET = 0

# Retried: transient. NOT retried: 401/403 (key or plan — retrying cannot fix it and burns budget),
# 402 (paid feature), 404 (no such symbol).
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3


class FmpError(RuntimeError):
    """Base for every FMP failure. Never carries the API key in its message."""


class FmpAuthError(FmpError):
    """401/403 — bad key, or an endpoint this plan cannot reach. Not retryable."""


class FmpPaywallError(FmpError):
    """402 — the endpoint exists and the plan does not include it (e.g. company-screener)."""


class FmpBudgetExhausted(FmpError):
    """The local daily call budget is spent. Raised BEFORE any request leaves the process."""


@dataclass
class CallBudget:
    """A process-local daily call counter.

    Deliberately local and approximate: it exists to stop a runaway loop from spending the day's
    allowance in one scan, not to mirror FMP's server-side accounting (which this key's responses
    do not expose — no X-RateLimit headers came back on probe). Approximate and visible beats exact
    and absent: `spent` is logged when the gate trips, so "the scan stopped early" is never a
    mystery.
    """

    limit: int = DEFAULT_DAILY_CALL_BUDGET
    spent: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def take(self, n: int = 1) -> None:
        """Consume n calls, or raise if that would exceed the budget. Atomic."""
        with self._lock:
            if self.limit > 0 and self.spent + n > self.limit:
                raise FmpBudgetExhausted(
                    f"FMP daily call budget exhausted: {self.spent}/{self.limit} used, "
                    f"{n} more requested. Raise FMP_DAILY_CALL_BUDGET or wait for the reset."
                )
            self.spent += n

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self.spent) if self.limit > 0 else 2**31


class MinuteRateGate:
    """Paces calls against the plan's per-minute ceiling by WAITING, not by refusing.

    The distinction matters. A quota being exhausted is a refusal — more calls today are simply not
    available. A rate limit is a speed limit: the work is still allowed, it just has to arrive
    slower. Raising here would turn "your 400-symbol backfill takes two minutes" into "your
    backfill died at symbol 271", which is the silently-blocking-guardrail failure this project has
    already paid for once.

    Blocks at most ``max_wait_s`` before giving up, so a wedged clock cannot hang a job forever.
    """

    def __init__(self, calls_per_minute: int = DEFAULT_CALLS_PER_MINUTE, max_wait_s: float = 90.0):
        self._limit = calls_per_minute
        self._max_wait_s = max_wait_s
        self._lock = threading.Lock()
        self._recent: deque[float] = deque()
        self.waited_total_s = 0.0  # observable: a job that paced is not a job that stalled

    def acquire(self) -> None:
        if self._limit <= 0:
            return
        deadline = time.monotonic() + self._max_wait_s
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - 60.0
                while self._recent and self._recent[0] <= cutoff:
                    self._recent.popleft()
                if len(self._recent) < self._limit:
                    self._recent.append(now)
                    return
                sleep_for = max(0.01, self._recent[0] + 60.0 - now)
            if time.monotonic() + sleep_for > deadline:
                raise FmpError(
                    f"FMP rate gate: still at {self._limit} calls/min after waiting "
                    f"{self._max_wait_s:.0f}s. Something is calling FMP outside this client, or "
                    f"the limit is set above the plan's."
                )
            logger.debug("FMP rate gate: pacing %.2fs", sleep_for)
            time.sleep(sleep_for)
            self.waited_total_s += sleep_for


def _int_from_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


def budget_from_env() -> CallBudget:
    """Optional hard daily cap from FMP_DAILY_CALL_BUDGET. Unset means NO daily cap.

    Deliberately the opposite default from the per-minute gate. On a plan metered per minute there
    is no daily allowance to protect, so refusing work at an invented daily number would throttle
    something the owner already paid for. Set this only to put a deliberate ceiling on a backfill.
    """
    return CallBudget(limit=_int_from_env("FMP_DAILY_CALL_BUDGET", DEFAULT_DAILY_CALL_BUDGET))


def rate_gate_from_env() -> MinuteRateGate:
    """The per-minute pacer. FMP_CALLS_PER_MINUTE overrides; the default sits under the plan."""
    return MinuteRateGate(_int_from_env("FMP_CALLS_PER_MINUTE", DEFAULT_CALLS_PER_MINUTE))


def load_api_key(raw: str | None = None) -> str:
    """The API key, from the argument or FMP_API_KEY. Never logged, never put in an exception."""
    key = raw if raw is not None else os.environ.get("FMP_API_KEY")
    if not key or not key.strip():
        raise FmpAuthError(
            "FMP_API_KEY is not set. Put it in backend/.env (mode 0600) — it is a paid credential."
        )
    return key.strip()


class FmpClient:
    """Thin, synchronous FMP client. One instance per job; safe to share across threads."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        budget: CallBudget | None = None,
        rate_gate: MinuteRateGate | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        base_url: str = BASE_URL,
    ) -> None:
        self._key = load_api_key(api_key)
        self.budget = budget if budget is not None else budget_from_env()
        self.rate_gate = rate_gate if rate_gate is not None else rate_gate_from_env()
        self._timeout_s = timeout_s
        self._base_url = base_url.rstrip("/")

    def _redact(self, text: str) -> str:
        """Strip the key from anything that might be logged or raised."""
        return text.replace(self._key, "<FMP_KEY>") if self._key else text

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        """GET one endpoint and return the decoded JSON.

        Consumes exactly one unit of budget per ATTEMPT, not per call: a retried request really did
        hit FMP's counter, and a budget that only counted successes would drift under exactly the
        conditions (throttling) where accuracy matters most.
        """
        import requests  # imported here so the module imports without the dep for unit tests

        query: dict[str, Any] = dict(params or {})
        query["apikey"] = self._key
        url = f"{self._base_url}/{endpoint.lstrip('/')}"

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self.rate_gate.acquire()  # pace first: waiting is not refusing
            self.budget.take(1)
            try:
                resp = requests.get(url, params=query, timeout=self._timeout_s)
            except Exception as exc:  # noqa: BLE001 — network shapes vary; all are retryable
                last_exc = exc
                logger.warning(
                    "FMP %s attempt %d/%d failed: %s",
                    endpoint,
                    attempt,
                    _MAX_ATTEMPTS,
                    self._redact(str(exc)),
                )
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(2 ** (attempt - 1))
                continue

            if resp.status_code == 402:
                raise FmpPaywallError(
                    f"FMP {endpoint} requires a paid plan (402). This key reaches the per-symbol "
                    f"endpoints; company-screener and other bulk endpoints are paid."
                )
            if resp.status_code in (401, 403):
                # 403 here is usually the retired /api/v3 surface, not a bad key — say so, because
                # "forbidden" sends people to regenerate a key that was never the problem.
                raise FmpAuthError(
                    f"FMP {endpoint} refused with {resp.status_code}. If the path is under "
                    f"/api/v3/, that surface is retired for accounts created after 2025-08-31 — "
                    f"use /stable/. Otherwise the key or plan is wrong."
                )
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                logger.warning(
                    "FMP %s returned %d, retrying (%d/%d)",
                    endpoint,
                    resp.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                )
                time.sleep(2 ** (attempt - 1))
                continue
            if not resp.ok:
                raise FmpError(f"FMP {endpoint} returned {resp.status_code}")

            try:
                return resp.json()
            except ValueError as exc:
                raise FmpError(f"FMP {endpoint} returned undecodable JSON") from exc

        raise FmpError(
            f"FMP {endpoint} failed after {_MAX_ATTEMPTS} attempts: {self._redact(str(last_exc))}"
        )

    # --- the five endpoints the screen needs ---------------------------------------------------
    # Each returns FMP's raw shape. Mapping into the screen's / the database's shape lives in
    # src/data.py, so the wire format and our schema stay separable — an FMP field rename should
    # break one mapping function with a clear name, not the whole ingest.

    def profile(self, symbol: str) -> dict | None:
        rows = self.get("profile", {"symbol": to_fmp_symbol(symbol)})
        return rows[0] if isinstance(rows, list) and rows else None

    def ratios(self, symbol: str, *, limit: int = 1, period: str = "annual") -> list[dict]:
        rows = self.get("ratios", {"symbol": to_fmp_symbol(symbol), "limit": limit, "period": period})
        return rows if isinstance(rows, list) else []

    def income_statement(self, symbol: str, *, limit: int = 1, period: str = "annual") -> list[dict]:
        rows = self.get("income-statement", {"symbol": to_fmp_symbol(symbol), "limit": limit, "period": period})
        return rows if isinstance(rows, list) else []

    def cash_flow(self, symbol: str, *, limit: int = 1, period: str = "annual") -> list[dict]:
        rows = self.get("cash-flow-statement", {"symbol": to_fmp_symbol(symbol), "limit": limit, "period": period})
        return rows if isinstance(rows, list) else []

    def growth(self, symbol: str, *, limit: int = 1, period: str = "annual") -> list[dict]:
        rows = self.get("financial-growth", {"symbol": to_fmp_symbol(symbol), "limit": limit, "period": period})
        return rows if isinstance(rows, list) else []

    CALLS_PER_SYMBOL = 5

    def fundamentals_bundle(self, symbol: str, *, periods: int = 1) -> dict[str, Any]:
        """Every payload needed to build one fundamentals row. 5 calls.

        Budget is checked for the WHOLE bundle up front: a symbol half-fetched because the budget
        ran out mid-way would be written with silently missing gates, which reads as "this company
        failed the screen" rather than "we ran out of API calls".
        """
        if self.budget.limit > 0 and self.budget.remaining() < self.CALLS_PER_SYMBOL:
            raise FmpBudgetExhausted(
                f"{self.budget.remaining()} FMP calls left, {self.CALLS_PER_SYMBOL} needed for "
                f"{symbol}; refusing to fetch a partial bundle"
            )
        ratios = self.ratios(symbol, limit=periods)
        income = self.income_statement(symbol, limit=periods)
        cash_flow = self.cash_flow(symbol, limit=periods)
        growth = self.growth(symbol, limit=periods)
        return {
            "profile": self.profile(symbol),
            # [0] is the most recent period — what the screen gates on.
            "ratios": (ratios or [None])[0],
            "income": (income or [None])[0],
            "cash_flow": (cash_flow or [None])[0],
            "growth": (growth or [None])[0],
            # Full lists, so the ingest can write one annual row PER period. Each carries its own
            # acceptedDate, which is what makes several periods real history rather than one figure
            # repeated: a backtest can ask what was knowable on any date and get a different answer.
            "periods": {
                "ratios": ratios,
                "income": income,
                "cash_flow": cash_flow,
                "growth": growth,
            },
        }


# ── the process-wide client ───────────────────────────────────────────────────────────────────
# One client per PROCESS, deliberately, because the rate gate is only a rate gate if everything
# shares it. Two clients means two independent 270/min budgets against one 300/min plan, and the
# overrun would appear as sporadic 429s under load — the hardest kind of bug to reproduce, because
# it needs two subsystems fetching at once. The backend has exactly that shape: the dashboard's
# marks poll and an operator's scan can run simultaneously.
_shared_client: FmpClient | None = None
_shared_lock = threading.Lock()


def get_shared_client() -> FmpClient:
    """The process-wide FMP client. Created on first use; safe from any thread."""
    global _shared_client
    if _shared_client is None:
        with _shared_lock:
            if _shared_client is None:
                _shared_client = FmpClient()
    return _shared_client


def reset_shared_client() -> None:
    """Drop the singleton. Test-support only — never called from request paths."""
    global _shared_client
    with _shared_lock:
        _shared_client = None


# Class-share symbols are spelled differently by the broker and the data vendor: Alpaca says
# "BRK.B", FMP says "BRK-B". Passing the broker's spelling straight to FMP returns an EMPTY result,
# not an error — so a held BRK.B would price as None and render as an unpriced position on the
# dashboard, with no indication that the cause was a punctuation mismatch rather than a market
# outage. Mapped in ONE place, at the boundary where the symbol crosses from our world into FMP's.
def to_fmp_symbol(symbol: str) -> str:
    """Our canonical symbol (Alpaca spelling) -> FMP's spelling."""
    return symbol.replace(".", "-")


def quote(symbol: str) -> dict | None:
    """Latest quote for one symbol via the shared client, or None if FMP has nothing."""
    rows = get_shared_client().get("quote", {"symbol": to_fmp_symbol(symbol)})
    return rows[0] if isinstance(rows, list) and rows else None
