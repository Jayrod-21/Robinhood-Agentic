"""An outage must not read as a decision.

FOUND LIVE, 2026-08-27. The Anthropic key hit its usage limit mid-debate. Every one of the ten
jurors failed, every one returned the engine's default `HOLD` at confidence 0.0, and the panel
reported:

    {'BUY': 0, 'SELL': 0, 'HOLD': 10} -> HOLD     "10/10 jurors voted HOLD — decisive majority."

A confident trading verdict manufactured out of an API outage, byte-identical in the record to ten
lenses that actually looked at the evidence. It is the same defect this project keeps finding — an
absent measurement impersonating a taken one — and the most consequential instance of it, because
the output is a decision about money.

Two changes, and these pin both: a juror that cannot answer ABSTAINS rather than voting, and a
panel below quorum ESCALATES rather than deciding.
"""

from __future__ import annotations

import pytest
from app.debate.aggregate import aggregate
from app.debate.schemas import Decision, JurorVote, Vote


def _vote(agent_id: int, vote: Vote, confidence: float = 0.8) -> JurorVote:
    return JurorVote(
        agent_id=agent_id, focus_area=f"lens{agent_id}", vote=vote,
        confidence=confidence, reasoning="r",
    )


# ── the incident ──────────────────────────────────────────────────────────────────────────────


def test_a_total_outage_escalates_rather_than_deciding() -> None:
    """THE test. Every juror abstained, so nothing reaches aggregate. Break: let an empty panel
    fall through to the plurality branch, and the outage reads as HOLD again."""
    result = aggregate([], jury_size=10)

    assert result.decision == Decision.ESCALATED
    assert result.escalated_to_human is True
    assert "not a HOLD" in result.reason


def test_the_old_behaviour_is_impossible_now() -> None:
    """Ten default HOLDs used to be a 'decisive majority'. They can no longer be constructed,
    because a failed juror never becomes a vote — but if one ever did, quorum is the second line."""
    result = aggregate([], jury_size=10)

    assert result.counts == {"BUY": 0, "SELL": 0, "HOLD": 0}
    assert result.decision != Decision.HOLD


@pytest.mark.parametrize("voted", [1, 2, 3, 4, 5])
def test_a_panel_below_half_does_not_return_a_verdict(voted: int) -> None:
    """Three of ten lenses is not a verdict, it is three opinions and seven unknowns."""
    result = aggregate([_vote(i, Vote.SELL) for i in range(1, voted + 1)], jury_size=10)

    assert result.decision == Decision.ESCALATED, f"{voted}/10 should not decide"
    assert f"Only {voted} of 10" in result.reason


def test_quorum_is_a_majority_of_the_intended_jury_not_of_the_survivors() -> None:
    """Six of ten is a real panel and decides normally; five is not."""
    six = aggregate([_vote(i, Vote.SELL) for i in range(1, 7)], jury_size=10)
    five = aggregate([_vote(i, Vote.SELL) for i in range(1, 6)], jury_size=10)

    assert six.decision == Decision.SELL
    assert five.decision == Decision.ESCALATED


def test_the_abstention_count_is_reported_not_just_implied() -> None:
    """An operator reading this needs to know the panel was short, not infer it from a small
    counts dict."""
    result = aggregate([_vote(1, Vote.BUY), _vote(2, Vote.BUY)], jury_size=10)

    assert any("8 of 10 jurors abstained" in s for s in result.calibration_signals)


def test_a_full_panel_is_unaffected() -> None:
    """Break: set the quorum floor above the jury size and every real debate escalates — an alarm
    that fires always is one nobody reads."""
    result = aggregate([_vote(i, Vote.SELL) for i in range(1, 11)], jury_size=10)

    assert result.decision == Decision.SELL
    assert result.escalated_to_human is False


# ── the juror itself ──────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_juror_that_cannot_answer_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a HOLD. HOLD is a judgement that the evidence supports doing nothing; an abstention is
    the absence of a judgement, and adding the two together is what produced the incident."""
    import asyncio

    from app.debate import anthropic_client as ac
    from app.debate import engine

    async def _dead(*_a, **_k):
        raise RuntimeError("You have reached your specified API usage limits.")

    # cast_vote_attributed, not cast_vote: the juror needs to know WHOSE key served the call so a
    # usage-limit rejection benches that owner rather than guessing between two configured keys.
    monkeypatch.setattr(ac, "cast_vote_attributed", _dead)
    result = await engine._run_juror(
        1, "valuation", "lens", "NVDA", 100.0, {}, "bull", "bear", "m", asyncio.Semaphore(1)
    )

    assert result is None


@pytest.mark.anyio
async def test_a_working_juror_still_votes(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from app.debate import anthropic_client as ac
    from app.debate import engine

    async def _ok(*_a, **_k):
        return {"vote": "BUY", "confidence": 0.83, "reasoning": "cheap"}, "Jared Anthropic"

    monkeypatch.setattr(ac, "cast_vote_attributed", _ok)
    vote = await engine._run_juror(
        3, "valuation", "lens", "NVDA", 100.0, {}, "bull", "bear", "m", asyncio.Semaphore(1)
    )

    assert vote is not None
    assert vote.vote == Vote.BUY and vote.confidence == 0.83


# ── failing over instead of failing twenty times ──────────────────────────────────────────────


def test_a_usage_limit_is_recognised_as_not_transient() -> None:
    """Anthropic returns it as a generic 400 BadRequestError with no distinct class, so it is
    matched on the message. A 400 is otherwise a programming error we must NOT fail over on."""
    from app.debate import anthropic_client as ac

    assert ac._is_usage_limit(
        RuntimeError("Error code: 400 - You have reached your specified API usage limits.")
    )
    assert not ac._is_usage_limit(RuntimeError("Error code: 400 - invalid tool schema"))
    assert not ac._is_usage_limit(RuntimeError("Connection reset by peer"))


def test_benching_an_owner_is_remembered_then_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process only, deliberately: the limit lifts on a date this code does not know, and a
    stale flag surviving a redeploy would keep a working key benched."""
    from app.debate import anthropic_client as ac

    monkeypatch.setattr(ac, "_EXHAUSTED", {})
    assert ac._exhausted("Jared Anthropic") is False

    ac.mark_exhausted("Jared Anthropic")
    assert ac._exhausted("Jared Anthropic") is True
    assert ac._exhausted("Joe Anthropic") is False, "one owner's limit is not the other's"

    # Expiry is time-based, not permanent.
    ac._EXHAUSTED["Jared Anthropic"] = 0.0
    assert ac._exhausted("Jared Anthropic") is False


@pytest.mark.anyio
async def test_a_usage_limit_stops_the_retry_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two attempts x ten jurors against an exhausted key is twenty guaranteed failures. The
    second attempt is skipped once the key is known to be out of budget."""
    import asyncio

    from app.debate import anthropic_client as ac
    from app.debate import engine

    calls = []

    async def _limited(*_a, **_k):
        calls.append(1)
        raise RuntimeError("You have reached your specified API usage limits.")

    monkeypatch.setattr(ac, "_EXHAUSTED", {})
    monkeypatch.setattr(ac, "cast_vote_attributed", _limited)
    result = await engine._run_juror(
        1, "valuation", "lens", "NVDA", 100.0, {}, "bull", "bear", "m", asyncio.Semaphore(1)
    )

    assert result is None
    assert len(calls) == 1, "the second attempt must be skipped, not spent"
