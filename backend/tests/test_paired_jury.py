"""The paired Claude/Gemini panel: the same ten lenses, judged by both families.

WHY PAIRED RATHER THAN SPLIT
    Splitting the ten lenses 5/5 between providers costs nothing extra and makes every disagreement
    unattributable — "macro says hold, valuation says sell" could be a lens difference or a model
    difference, and no two jurors would ever answer the same question. Running both families over
    the SAME lens buys the only clean comparison available: same evidence, same lens, two families.

    Measured on the first real paired run: the families disagreed on `growth` (Claude BUY / Gemini
    HOLD) and `valuation` (Claude HOLD / Gemini SELL), and agreed on the other four paired lenses.
    That is the finding the design exists to produce.
"""

from __future__ import annotations

import pytest
from app.debate import calibration
from app.debate.schemas import JurorVote, Vote


def _v(agent_id: int, lens: str, vote: Vote, provider: str, conf: float = 0.7) -> JurorVote:
    return JurorVote(
        agent_id=agent_id, focus_area=lens, vote=vote, confidence=conf,
        reasoning="r", provider=provider, model=f"{provider}-model",
    )


def _paired(pairs: dict[str, tuple[Vote, Vote]]) -> list[JurorVote]:
    """{lens: (anthropic_vote, gemini_vote)} -> a paired panel."""
    votes = []
    for i, (lens, (a, g)) in enumerate(pairs.items(), start=1):
        votes.append(_v(i, lens, a, "anthropic"))
        votes.append(_v(100 + i, lens, g, "gemini"))
    return votes


# ── a family split is a finding, not a tie ────────────────────────────────────────────────────


def test_families_reaching_different_verdicts_is_reported_as_a_finding() -> None:
    """THE point of pairing. A 10-10 split along family lines says the question is model-dependent,
    which is the most informative thing this panel can produce. Break: resolve it to HOLD and the
    information is gone."""
    votes = _paired({f"lens{i}": (Vote.BUY, Vote.SELL) for i in range(1, 6)})
    signals = calibration.family_signals(votes)

    assert any("DIFFERENT verdicts" in s for s in signals)
    assert any("model-dependent" in s for s in signals)


def test_low_per_lens_agreement_is_flagged_with_the_lenses_named() -> None:
    """Naming which lenses disagreed is the actionable part — "valuation" disagreeing is a
    different investigation from "sentiment" disagreeing."""
    votes = _paired({
        "valuation": (Vote.HOLD, Vote.SELL),
        "growth": (Vote.BUY, Vote.HOLD),
        "macro_rates": (Vote.HOLD, Vote.SELL),
        "cash_flow": (Vote.HOLD, Vote.HOLD),
    })
    signals = calibration.family_signals(votes)

    assert any("3 of 4 paired lenses" in s for s in signals)
    assert any("valuation" in s and "growth" in s for s in signals)


def test_families_that_agree_are_not_flagged() -> None:
    """Break: flag on any paired panel. An alarm that fires every debate is one nobody reads."""
    votes = _paired({f"lens{i}": (Vote.HOLD, Vote.HOLD) for i in range(1, 6)})

    assert calibration.family_signals(votes) == []


def test_a_single_family_panel_produces_no_family_signals() -> None:
    """There is nothing to compare. Every debate before this change was single-family, and none of
    them should retroactively grow a warning."""
    votes = [_v(i, f"lens{i}", Vote.HOLD, "anthropic") for i in range(1, 11)]

    assert calibration.family_signals(votes) == []
    assert calibration.family_summary(votes)["paired_lenses"] == 0


# ── the summary that makes this answerable from history ───────────────────────────────────────


def test_the_summary_records_agreement_per_lens() -> None:
    """Until there are enough paired debates on record, a cross-family AGREEMENT is not extra
    confidence — it may only mean the question was easy. The summary is what makes that
    measurable later rather than a matter of impression."""
    votes = _paired({
        "valuation": (Vote.HOLD, Vote.SELL),
        "growth": (Vote.BUY, Vote.HOLD),
        "cash_flow": (Vote.HOLD, Vote.HOLD),
        "tail_risk": (Vote.SELL, Vote.SELL),
    })
    summary = calibration.family_summary(votes)

    assert summary["paired_lenses"] == 4
    assert summary["lenses_agreed"] == 2
    assert summary["agreement"] == 0.5
    assert summary["disagreed_on"] == ["growth", "valuation"]
    assert summary["providers"]["anthropic"] == {"BUY": 1, "SELL": 1, "HOLD": 2}
    assert summary["providers"]["gemini"] == {"BUY": 0, "SELL": 2, "HOLD": 2}


def test_an_unpaired_lens_is_excluded_from_agreement() -> None:
    """A lens only one family reached — the other abstained — cannot be compared, and counting it
    as agreement would overstate how much the families actually line up."""
    votes = _paired({"valuation": (Vote.HOLD, Vote.SELL)})
    votes.append(_v(9, "lonely", Vote.BUY, "anthropic"))
    summary = calibration.family_summary(votes)

    assert summary["paired_lenses"] == 1, "the unpaired lens is not a comparison"


# ── the vote carries its family ───────────────────────────────────────────────────────────────


def test_a_vote_records_which_family_cast_it() -> None:
    """Without it a disagreement is unattributable: "the models disagree about NVDA" cannot be
    told apart from "Gemini is systematically more bearish", and the second is an artifact."""
    vote = _v(1, "valuation", Vote.SELL, "gemini")

    assert vote.provider == "gemini"
    assert vote.model == "gemini-model"


def test_historical_votes_still_parse_without_a_provider() -> None:
    """195 debates predate this field. They default to anthropic, which is what they were."""
    old = JurorVote(agent_id=1, focus_area="valuation", vote=Vote.HOLD, confidence=0.72, reasoning="r")

    assert old.provider == "anthropic"


# ── quorum on a 20-seat panel ─────────────────────────────────────────────────────────────────


def test_one_family_entirely_unreachable_does_not_decide_alone() -> None:
    """Flagged when the paired panel was designed: a 20-seat jury where one family is completely
    down leaves 10 votes, which is BELOW a majority of 20 — so it escalates rather than quietly
    returning a single-family verdict dressed as a paired one."""
    from app.debate.aggregate import aggregate
    from app.debate.schemas import Decision

    anthropic_only = [_v(i, f"lens{i}", Vote.SELL, "anthropic") for i in range(1, 11)]
    result = aggregate(anthropic_only, jury_size=20)

    assert result.decision == Decision.ESCALATED
    assert "Only 10 of 20" in result.reason


@pytest.mark.parametrize("gemini_votes", [1, 2, 3])
def test_a_mostly_intact_paired_panel_still_decides(gemini_votes: int) -> None:
    """Break: require both families to be complete. A couple of rate-limited Gemini seats would
    then escalate every debate, which is an alarm nobody reads."""
    from app.debate.aggregate import aggregate
    from app.debate.schemas import Decision

    votes = [_v(i, f"lens{i}", Vote.SELL, "anthropic") for i in range(1, 11)]
    votes += [_v(100 + i, f"lens{i}", Vote.SELL, "gemini") for i in range(1, gemini_votes + 1)]
    result = aggregate(votes, jury_size=20)

    expected = Decision.SELL if len(votes) >= 11 else Decision.ESCALATED
    assert result.decision == expected
