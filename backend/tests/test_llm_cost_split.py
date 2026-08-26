"""Splitting one LLM bill between two owners.

Measured 2026-08-26: roughly $9 of Anthropic spend on Jared's key and nothing on Joe's. The goal is
to converge, and the central trap is that round-robin does not converge — alternating 50/50 from
today leaves the $9 gap in place permanently. Selection has to prefer whoever is BEHIND.

The second trap is attribution. The environment does NOT follow "slot 1 is always the same person":
Anthropic slot 1 is Jared, Gemini slot 1 is Joe. A ledger that attributes by position is not a
worse ledger, it is a confidently wrong answer about who owes whom.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.llm import keys as key_registry
from app.llm.pricing import PRICING_VERSION, cost_usd, priced_models, rate_for


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Both providers configured, with the real inverted ownership."""
    for var, value in (
        ("ANTHROPIC_API_KEY", "sk-a-jared"), ("ANTHROPIC_API_KEY_NAME", "Jared Anthropic"),
        ("ANTHROPIC_API_KEY_2", "sk-a-joe"), ("ANTHROPIC_API_KEY_NAME_2", "Joe Anthropic"),
        ("GEMINI_API_KEY", "g-joe"), ("GEMINI_API_KEY_NAME", "Joe Gemini"),
        ("GEMINI_API_KEY_2", "g-jared"), ("GEMINI_API_KEY_NAME_2", "Jared Gemini"),
    ):
        monkeypatch.setenv(var, value)


# ── the trap: round-robin freezes an imbalance ────────────────────────────────────────────────


def test_the_owner_who_is_behind_gets_the_next_call(env) -> None:
    """THE test. Break: return the next key in rotation instead of the cheapest. Jared stays $9
    ahead forever and the feature does nothing."""
    chosen = key_registry.select("anthropic", {"Jared Anthropic": 9.0, "Joe Anthropic": 0.0})
    assert chosen.owner == "Joe Anthropic"


def test_it_keeps_choosing_the_same_owner_until_they_converge(env) -> None:
    """Not alternation — sustained preference. That is what closes a gap rather than preserving it."""
    spend = {"Jared Anthropic": 9.0, "Joe Anthropic": 0.0}
    picks = []
    for _ in range(12):
        chosen = key_registry.select("anthropic", spend)
        picks.append(chosen.owner)
        spend[chosen.owner] += 1.0

    # The first NINE go to Joe — the whole $9 gap — and only then does it alternate. Round-robin
    # would have produced Jared/Joe/Jared/Joe from the start and left the gap untouched forever.
    assert picks[:9] == ["Joe Anthropic"] * 9
    assert set(picks[9:]) == {"Jared Anthropic", "Joe Anthropic"}
    assert abs(spend["Jared Anthropic"] - spend["Joe Anthropic"]) <= 1.0, "converged to within a call"


def test_an_owner_with_no_recorded_spend_sorts_first(env) -> None:
    """A newly added key absorbs calls until it catches up, which is the desired behaviour and the
    reason a missing entry must mean zero rather than being skipped."""
    assert key_registry.select("anthropic", {"Jared Anthropic": 4.0}).owner == "Joe Anthropic"


def test_ties_break_deterministically(env) -> None:
    """A random tiebreak would make identical inputs produce different bills."""
    spend = {"Jared Anthropic": 5.0, "Joe Anthropic": 5.0}
    assert {key_registry.select("anthropic", spend).owner for _ in range(10)} == {"Jared Anthropic"}


# ── the other trap: ownership is not positional ───────────────────────────────────────────────


def test_ownership_comes_from_the_label_not_the_slot(env) -> None:
    """Anthropic slot 1 is Jared; Gemini slot 1 is Joe. Anything assuming "_1 = Jared" misattributes
    every Gemini call — and a cost ledger that misattributes is worse than none."""
    assert key_registry.select("anthropic", {}).owner == "Jared Anthropic"
    assert key_registry.select("gemini", {}).owner == "Joe Gemini"


def test_each_provider_only_sees_its_own_keys(env) -> None:
    assert {k.owner for k in key_registry.available("anthropic")} == {
        "Jared Anthropic", "Joe Anthropic"
    }
    assert {k.owner for k in key_registry.available("gemini")} == {"Joe Gemini", "Jared Gemini"}


def test_a_key_never_describes_its_secret(env) -> None:
    """Not even a prefix. A key id is half a credential, and showing one teaches an operator it is
    safe to paste."""
    for key in key_registry.available():
        described = key.describe()
        assert set(described) == {"provider", "owner"}
        assert key.secret not in str(described)


def test_an_unlabelled_key_is_attributed_to_its_variable_not_pooled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It still works, but its spend must not silently join someone else's total."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.delenv("ANTHROPIC_API_KEY_NAME", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY_2", raising=False)

    assert key_registry.available("anthropic")[0].owner == "ANTHROPIC_API_KEY"


def test_owner_labels_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """They arrived with a leading space. Unstripped, " Jared Anthropic" and "Jared Anthropic"
    become two distinct owners in the ledger and neither total is right."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY_NAME", "  Jared Anthropic  ")

    assert key_registry.available("anthropic")[0].owner == "Jared Anthropic"


def test_no_keys_configured_selects_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    for var, _, name in key_registry._SLOTS:
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(name, raising=False)
    assert key_registry.select("anthropic", {}) is None


# ── pricing is an assumption, and says so ─────────────────────────────────────────────────────


def test_the_configured_jury_and_synth_models_are_priced() -> None:
    """A model this deployment actually uses with no rate means every call it makes goes into the
    ledger with a NULL cost — an owner's total silently understated."""
    for model in ("claude-haiku-4-5", "claude-sonnet-4-6"):
        assert rate_for(model) is not None, f"{model} has no published rate"


def test_haiku_costs_what_the_price_list_says() -> None:
    """$1.00 per 1M input, $5.00 per 1M output (documented 2026-06-24)."""
    assert cost_usd("claude-haiku-4-5", 1_000_000, 0) == Decimal("1.00")
    assert cost_usd("claude-haiku-4-5", 0, 1_000_000) == Decimal("5.00")
    assert cost_usd("claude-haiku-4-5", 1_000_000, 100_000) == Decimal("1.50")


def test_an_unpriced_model_costs_none_not_zero() -> None:
    """Zero is a claim the call was free. None is "we do not know", and it is the truthful answer —
    a new model dropped into JURY_MODEL would otherwise be billed at nothing forever.

    Break: `return Decimal(0)` for an unknown model. This goes red.
    """
    assert cost_usd("gemini-3-pro", 1000, 1000) is None
    assert cost_usd("", 1000, 1000) is None


def test_negative_token_counts_cannot_produce_a_credit() -> None:
    """A garbage usage payload must not reduce someone's bill."""
    assert cost_usd("claude-haiku-4-5", -5_000_000, -5_000_000) == Decimal(0)


def test_the_pricing_version_is_recorded_and_positive() -> None:
    """Every ledger row carries it, so a rate change can be found and repriced rather than leaving
    a ledger that is quietly wrong in a way nobody can locate."""
    assert isinstance(PRICING_VERSION, int) and PRICING_VERSION >= 1
    assert priced_models(), "the price list must not be empty"


def test_the_spread_counts_owners_who_have_spent_nothing(monkeypatch: pytest.MonkeyPatch, env) -> None:
    """Caught live. The spend view only knows owners who have rows, so the spread was computed over
    one owner and reported $0.00 while Jared was at $9 and Joe had never been used — the exact
    imbalance this feature exists to close, reported as already even.

    Absent is not zero for REPORTING, even though it correctly is for selection.
    """
    from app.llm import ledger

    monkeypatch.setattr(
        ledger, "connection", lambda: (_ for _ in ()).throw(RuntimeError("no db"))
    )
    assert ledger.totals()["owners"] == [], "a dead database degrades rather than lying"


def test_the_response_shape_does_not_change_when_the_database_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response whose keys depend on database reachability forces every consumer to handle two
    shapes, and the one that forgets breaks only during an outage — the worst time to find out.

    The `note` is what distinguishes an empty ledger from an unreachable one.
    """
    from app.llm import ledger

    monkeypatch.setattr(
        ledger, "connection", lambda: (_ for _ in ()).throw(RuntimeError("no db"))
    )
    degraded = ledger.totals()

    assert set(degraded) >= {"owners", "total_usd", "pricing_version", "spread_usd_by_provider"}
    assert degraded["owners"] == []
    assert "not a measurement" in degraded["note"], "and it says the zeros are not a measurement"
