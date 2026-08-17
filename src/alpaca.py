"""Alpaca client — the ACCOUNT OF RECORD for positions, cash, and cost basis.

WHY THIS EXISTS
    FMP knows what companies are worth. It cannot know what you own, and no amount of engineering
    changes that — a market-data vendor has no relationship with your brokerage. Only the broker
    knows the holdings, and until now the only route to them was a JSON file written by a host-side
    daemon driving a Claude session against the Robinhood MCP. That file is three weeks stale at the
    time of writing, and nothing in the UI said so.

    Alpaca exposes positions, cash and cost basis over a plain authenticated REST API. No session in
    the loop, no aggregator holding credentials, no scrape.

PAPER VERSUS LIVE IS ONE VARIABLE, DELIBERATELY
    ``ALPACA_BASE_URL`` is the ONLY thing separating paper from live trading. It is explicit rather
    than inferred from whether a key "looks" live, because the inferred version fails silently in
    the worst direction: a misread key prefix would point real orders at a live account while every
    log line still said paper. :func:`assert_paper` exists so a caller can demand the paper
    endpoint and get a refusal rather than a surprise.

WHAT THIS DOES NOT DO
    It does not place orders. This module reads. The dashboard is read-only by design (SECURITY.md),
    and the day an execution path is added it will be a separate, deliberately-named module with its
    own guardrails — not an extra method quietly appended here.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("agentic.alpaca")

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"
DEFAULT_TIMEOUT_S = 15.0

# Retried: transient. NOT retried: 401/403 (credentials — retrying cannot fix them).
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3


class AlpacaError(RuntimeError):
    """Base for every Alpaca failure. Never carries the key or secret in its message."""


class AlpacaAuthError(AlpacaError):
    """401/403 — bad key/secret, or a key issued for the other environment."""


class AlpacaNotPaper(AlpacaError):
    """The configured base URL is not the paper endpoint, and the caller required paper."""


def load_credentials(
    key_id: str | None = None, secret: str | None = None
) -> tuple[str, str]:
    """Key id and secret, from arguments or the environment. Neither is ever logged."""
    kid = key_id if key_id is not None else os.environ.get("ALPACA_API_KEY_ID")
    sec = secret if secret is not None else os.environ.get("ALPACA_API_SECRET_KEY")
    if not kid or not kid.strip() or not sec or not sec.strip():
        raise AlpacaAuthError(
            "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set. Generate a PAPER key pair at "
            "alpaca.markets and put both in backend/.env (mode 0600). The secret is shown once."
        )
    return kid.strip(), sec.strip()


def normalize_base_url(raw: str | None) -> str:
    """Accept what Alpaca's dashboard actually shows, and return what the client can build on.

    The dashboard displays the endpoint WITH the version segment —
    ``https://paper-api.alpaca.markets/v2`` — and that is what an operator copies. This client
    builds paths as ``{base}/v2/account``, so pasting it verbatim would request
    ``/v2/v2/account`` and 404.

    The nastier half is what it did to :func:`is_paper`, which compared the string exactly: a
    trailing ``/v2`` made a PAPER endpoint fail the paper test, so the snapshot would have been
    labelled ``alpaca-live`` and ``assert_paper`` would have refused a paper account. A value that
    reports the opposite of what it is beats a 404 for damage every time.

    So both forms are accepted and normalised to the bare origin.
    """
    value = (raw or "").strip().rstrip("/")
    if not value:
        return PAPER_BASE_URL
    if value.lower().endswith("/v2"):
        value = value[: -len("/v2")].rstrip("/")
    return value


def base_url_from_env() -> str:
    """The configured endpoint. Defaults to PAPER — the safe direction if nobody set it."""
    return normalize_base_url(os.environ.get("ALPACA_BASE_URL"))


def is_paper(base_url: str) -> bool:
    """True when the endpoint is Alpaca's paper host.

    Compares the HOST, not the string: scheme, trailing slash and the ``/v2`` suffix are all
    presentation, and an operator pasting any of those variants must not silently be told they are
    on live.
    """
    from urllib.parse import urlparse

    host = urlparse(normalize_base_url(base_url)).hostname or ""
    return host.lower() == (urlparse(PAPER_BASE_URL).hostname or "")


class AlpacaClient:
    """Read-only Alpaca REST client."""

    def __init__(
        self,
        key_id: str | None = None,
        secret: str | None = None,
        *,
        base_url: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._key_id, self._secret = load_credentials(key_id, secret)
        self.base_url = normalize_base_url(base_url) if base_url else base_url_from_env()
        self._timeout_s = timeout_s

    @property
    def is_paper(self) -> bool:
        return is_paper(self.base_url)

    def assert_paper(self) -> None:
        """Refuse to proceed unless pointed at the paper endpoint.

        For callers that must never touch a funded account — seeding, test harnesses, anything a
        developer runs casually. The check is on the URL because the URL is what routes the request;
        a key that "looks like" a paper key is a guess, and this is not a place to guess.
        """
        if not self.is_paper:
            raise AlpacaNotPaper(
                f"ALPACA_BASE_URL is {self.base_url!r}, not the paper endpoint "
                f"({PAPER_BASE_URL}). Refusing: this caller requires paper."
            )

    def _redact(self, text: str) -> str:
        for value in (self._secret, self._key_id):
            if value:
                text = text.replace(value, "<ALPACA_REDACTED>")
        return text

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        import requests

        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "APCA-API-KEY-ID": self._key_id,
            "APCA-API-SECRET-KEY": self._secret,
            "accept": "application/json",
        }
        last: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=self._timeout_s)
            except Exception as exc:  # noqa: BLE001 — network shapes vary; all retryable
                last = exc
                logger.warning(
                    "alpaca %s attempt %d/%d failed: %s",
                    path, attempt, _MAX_ATTEMPTS, self._redact(str(exc)),
                )
                continue
            if resp.status_code in (401, 403):
                raise AlpacaAuthError(
                    f"Alpaca refused {path} with {resp.status_code}. Check the key pair, and that "
                    f"it was issued for THIS environment — paper keys do not work against live, "
                    f"or the reverse. Configured endpoint: {self.base_url}"
                )
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                logger.warning("alpaca %s returned %d, retrying", path, resp.status_code)
                continue
            if not resp.ok:
                raise AlpacaError(f"Alpaca {path} returned {resp.status_code}")
            try:
                return resp.json()
            except ValueError as exc:
                raise AlpacaError(f"Alpaca {path} returned undecodable JSON") from exc
        raise AlpacaError(f"Alpaca {path} failed after {_MAX_ATTEMPTS} attempts: {self._redact(str(last))}")

    def account(self) -> dict:
        return self.get("/v2/account")

    def positions(self) -> list[dict]:
        rows = self.get("/v2/positions")
        return rows if isinstance(rows, list) else []


def _num(value: Any) -> float | None:
    """Alpaca returns numerics as STRINGS ("1234.56"). Coerce, treating junk as missing."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def snapshot_from_alpaca(
    account: dict, positions: list[dict], *, generated_at: datetime | None = None
) -> dict:
    """Map Alpaca's account + positions into the dashboard's existing snapshot shape.

    Deliberately produces the SAME contract the Robinhood file produces
    (backend/app/services/snapshot.py::AccountSnapshot), so the dashboard, the marks overlay and
    the P&L math are untouched by the broker swap. The broker is an implementation detail below a
    contract that already existed; inventing a second shape would fork every consumer.

    ``source`` distinguishes them — "alpaca-paper" vs "alpaca-live" vs "robinhood-mcp" — so the UI
    can say which account it is showing rather than leaving the operator to assume.

    FIELD MAPPING, with the traps named:
      * Alpaca returns every numeric as a STRING. Passing them through would give Pydantic a str
        where it wants a float, or worse, string-concatenate in arithmetic.
      * ``equity`` is total account value (positions + cash); ``long_market_value`` is the
        positions-only figure the dashboard calls equity_value. Mapping equity -> equity_value
        would double-count cash in the allocation chart.
      * ``qty`` is signed: negative for shorts. The snapshot contract requires quantity > 0, so
        short positions are DROPPED here with a warning rather than silently failing validation
        for the whole snapshot. This account is long-only today; a short arriving means the
        contract needs widening, and the warning is how anyone finds out.
    """
    stamp = generated_at or datetime.now(timezone.utc)
    equity = _num(account.get("equity")) or 0.0
    long_mv = _num(account.get("long_market_value"))
    cash = _num(account.get("cash")) or 0.0

    mapped: list[dict] = []
    for row in positions:
        qty = _num(row.get("qty"))
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol or qty is None:
            logger.warning("alpaca position skipped: unusable symbol/qty %r", row.get("symbol"))
            continue
        if qty <= 0:
            logger.warning(
                "alpaca position %s has qty %s (short or closed); the snapshot contract is "
                "long-only, so it is omitted rather than failing the whole snapshot",
                symbol, qty,
            )
            continue
        mapped.append(
            {
                "symbol": symbol,
                "quantity": qty,
                "average_buy_price": _num(row.get("avg_entry_price")) or 0.0,
                "intraday_quantity": 0.0,
            }
        )

    account_number = str(account.get("account_number") or "")
    masked = f"••••{account_number[-4:]}" if len(account_number) >= 4 else "••••????"

    return {
        "schema_version": 1,
        "source": "alpaca-paper" if is_paper(base_url_from_env()) else "alpaca-live",
        "generated_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "account": {
            "number_masked": masked,
            "nickname": "Alpaca paper" if is_paper(base_url_from_env()) else "Alpaca",
            "total_value": equity,
            # positions-only; falls back to equity - cash when Alpaca omits long_market_value
            "equity_value": long_mv if long_mv is not None else max(0.0, equity - cash),
            "cash": cash,
            "buying_power": _num(account.get("buying_power")) or 0.0,
            "currency": account.get("currency") or "USD",
        },
        "positions": mapped,
    }


def fetch_snapshot(client: AlpacaClient | None = None) -> dict:
    """One live read of the account of record, in the dashboard's snapshot shape."""
    c = client or AlpacaClient()
    return snapshot_from_alpaca(c.account(), c.positions())
