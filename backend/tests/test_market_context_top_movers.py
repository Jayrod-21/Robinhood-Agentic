"""The Top Movers passthrough, and the two facts the backend adds to it.

Market Mover publishes `top_movers` pre-shaped — {rank, ticker, category, title, justification,
verdict} — so the route relays it the way it already relays `headlines`. Two things are deliberate:

  * `verdict` is passed through UNMODIFIED. It is null today, because the brief records impact and
    not a directional call. Normalising it to a default here would put a call in front of an
    operator that nobody made.
  * `held` and `in_slate` are ADDED. Per the contract those are the backend's to answer, and they
    are what makes a ranked mover actionable rather than trivia.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.routers import market_context as mod

BRIEF: dict[str, Any] = {
    "schema_version": 1,
    "generated_at": "2026-08-26T12:00:00Z",
    "brief_date": "2026-08-26",
    "macro_read": "rates steady",
    "headlines": [],
    "top_movers": [
        {"rank": 1, "ticker": "nvda", "category": "AI", "title": "T1",
         "justification": "J1", "verdict": None},
        {"rank": 2, "ticker": "TSM", "category": "Semis", "title": "T2",
         "justification": "J2", "verdict": "bullish"},
        {"rank": 3, "ticker": "ZZZZ", "category": "Other", "title": "T3",
         "justification": "J3", "verdict": None},
    ],
}


@pytest.fixture
def route(monkeypatch: pytest.MonkeyPatch):
    """The route with a canned brief, a known slate, and a known set of holdings."""

    def _install(brief):
        monkeypatch.setattr(mod, "_load_brief", lambda: brief)
        monkeypatch.setattr(mod, "_catalysts", lambda *a, **k: [])
        # Returns the (slate, path, status) triple load_governing_slate answers with: the route
        # asks "which targets govern this account", not "parse this file".
        monkeypatch.setattr(
            mod, "load_governing_slate",
            lambda *a, **k: ({"TSM": object(), "NVDA": object()}, None, None),
        )

        class _Snap:
            positions = [type("P", (), {"symbol": "NVDA"})()]

        monkeypatch.setattr(mod, "get_snapshot", lambda *a, **k: _Snap())
        return mod.market_context()

    return _install


def test_the_movers_are_relayed_in_order_with_their_fields(route) -> None:
    movers = route(BRIEF)["top_movers"]

    assert [m["rank"] for m in movers] == [1, 2, 3]
    assert [m["category"] for m in movers] == ["AI", "Semis", "Other"]
    assert movers[0]["justification"] == "J1"


def test_a_null_verdict_stays_null(route) -> None:
    """Break: default it to "neutral". The page maps recognised words to a tone and shows no badge
    for null — a default would show a badge for a call the brief never made."""
    movers = route(BRIEF)["top_movers"]

    assert movers[0]["verdict"] is None
    assert movers[1]["verdict"] == "bullish", "and a real verdict is not normalised either"


def test_tickers_are_upper_cased_so_held_matching_works(route) -> None:
    """The brief sent "nvda". Without folding case, the held/in_slate join silently misses."""
    movers = route(BRIEF)["top_movers"]

    assert movers[0]["ticker"] == "NVDA"


def test_held_and_in_slate_are_added_by_the_backend(route) -> None:
    """Contract: these are 'fields the backend owns'. NVDA is held and in the slate; TSM is in the
    slate but not held; ZZZZ is neither."""
    by_ticker = {m["ticker"]: m for m in route(BRIEF)["top_movers"]}

    assert (by_ticker["NVDA"]["held"], by_ticker["NVDA"]["in_slate"]) == (True, True)
    assert (by_ticker["TSM"]["held"], by_ticker["TSM"]["in_slate"]) == (False, True)
    assert (by_ticker["ZZZZ"]["held"], by_ticker["ZZZZ"]["in_slate"]) == (False, False)


def test_a_brief_with_no_movers_yields_an_empty_list_not_a_missing_key(route) -> None:
    """The contract permits omitting the key, but an absent key and an empty list read identically
    to the page — and they are different facts. `[]` means the brief carried none."""
    response = route({**BRIEF, "top_movers": []})

    assert response["top_movers"] == []
    assert "top_movers" in response


def test_no_brief_at_all_still_yields_the_key(route) -> None:
    response = route(None)

    assert response["top_movers"] == []
    assert response["meta"]["brief_present"] is False, (
        "and meta says WHY it is empty — no brief, as opposed to a brief with no movers"
    )


def test_a_malformed_movers_payload_does_not_take_the_page_down(route) -> None:
    """Third-party text. A string where a list belongs, or a string inside the list, must not 500
    the Market page — the rest of the response is still useful."""
    for bad in ("not a list", 42, {"rank": 1}):
        response = route({**BRIEF, "top_movers": bad})
        assert response["top_movers"] == []

    mixed = route({**BRIEF, "top_movers": ["junk", BRIEF["top_movers"][0], None]})
    assert len(mixed["top_movers"]) == 1, "the one well-formed entry survives"


def test_a_mover_with_no_ticker_is_relayed_rather_than_dropped(route) -> None:
    """A macro mover with no symbol is still a ranked mover. It cannot be held or in the slate."""
    response = route({**BRIEF, "top_movers": [
        {"rank": 1, "category": "Macro", "title": "CPI", "justification": "J", "verdict": None}
    ]})

    mover = response["top_movers"][0]
    assert mover["ticker"] is None
    assert mover["held"] is False and mover["in_slate"] is False
