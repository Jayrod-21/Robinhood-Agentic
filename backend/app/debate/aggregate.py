"""Deterministic jury aggregation — ports 3a's consensus rules.

Rules (denominator ``n`` = the number of votes actually cast, normally ``jury_size`` = 10):
- A clear majority (``n // 2 + 1`` votes for one action) is DECISIVE.
- An exact even split between the two *directional, opposed* actions (e.g. 5 BUY / 5 SELL of 10) is
  ESCALATED to a human and is NEVER auto-resolved.
- Anything else (a plurality short of a majority) falls to HOLD: no conviction.

Divergence from 3a: 3a escalates *any* 5-5 split. Here, escalation is restricted to a true
directional deadlock — BUY and SELL each at exactly ``n/2`` (even juries only). A tie that includes
HOLD (e.g. 5 BUY / 5 HOLD) is NOT a deadlock: half the jury already chose the conservative no-action
verdict, so it resolves to HOLD rather than paging a human. For a live, real-money account the
conservative default on a no-conviction split is to stand down, not to escalate every even split.
"""

from __future__ import annotations

import logging

from app.debate.schemas import Decision, JurorVote, JuryResult

logger = logging.getLogger("agentic.debate.aggregate")


def counts_of(votes: list[JurorVote]) -> dict[str, int]:
    """Tally, shared by the quorum path and the main path so they cannot disagree."""
    counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for v in votes:
        counts[v.vote.value] += 1
    return counts


def _no_quorum_result(
    votes: list[JurorVote], counts: dict[str, int], jury_size: int, n: int, quorum: int
) -> JuryResult:
    """The result when too few jurors could be reached to call anything.

    ESCALATED, not HOLD. HOLD is a judgement that the evidence supports doing nothing; this is the
    absence of a judgement, and rendering it as HOLD is what let an outage read as a decision.
    """
    from app.debate import calibration

    logger.error(
        "jury did not reach quorum: %d of %d voted, %d needed — escalating rather than deciding",
        n, jury_size, quorum,
    )
    return JuryResult(
        votes=votes,
        counts=counts,
        decision=Decision.ESCALATED,
        escalated_to_human=True,
        reason=(
            f"Only {n} of {jury_size} jurors returned a vote ({quorum} needed). The rest could not "
            f"be reached. This is not a HOLD — it is an absent verdict, and it needs a human."
        ),
        calibration_signals=[
            f"{jury_size - n} of {jury_size} jurors abstained — the panel could not be assembled"
        ],
        confidence=calibration.confidence_summary(votes),
    )


def aggregate(votes: list[JurorVote], jury_size: int) -> JuryResult:
    # The denominator for BOTH the majority threshold and the directional-deadlock test is the number
    # of votes actually cast — the real jury. Previously the majority used ``jury_size`` while the tie
    # test used ``len(votes)``; those agree only because the engine returns exactly ``jury_size``
    # votes today. Keying everything off ``len(votes)`` makes the two consistent by construction and
    # robust if a caller ever passes a short list (e.g. ``jury_size`` configured above the number of
    # available juror perspectives). We log — but never crash on — a mismatch so a genuine engine
    # regression is visible.
    n = len(votes)
    if n != jury_size:
        logger.warning("aggregate got %d votes but jury_size=%d; using %d as the basis", n, jury_size, n)

    # A PANEL TOO SMALL TO JUDGE DOES NOT JUDGE.
    #
    # Before jurors could abstain, a total API outage produced ten default HOLD votes and this
    # function reported "10/10 jurors voted HOLD — decisive majority": a confident trading verdict
    # manufactured from an outage. Jurors abstain now, so that same outage arrives here as an empty
    # list — and an empty list must not fall through the plurality branch to HOLD, which would
    # rebuild the exact same lie one layer up.
    #
    # The floor is a majority of the INTENDED jury: a verdict from three of ten lenses is not a
    # verdict, it is three opinions with seven unknowns.
    quorum = max(1, (jury_size // 2) + 1) if jury_size else 1
    if n < quorum:
        return _no_quorum_result(votes, counts_of(votes), jury_size, n, quorum)

    # Computed here so every caller of aggregate() gets it, rather than each one remembering to
    # ask. It annotates; it never changes the verdict below — see calibration.py.
    from app.debate import calibration

    calibration_signals = calibration.signals(votes)
    if calibration_signals:
        logger.warning("jury calibration: %s", "; ".join(calibration_signals))
    # Spread onto every return below. A shared dict rather than three copied pairs, so a future
    # early-return cannot quietly ship a JuryResult with the annotations missing — which would
    # render as "this panel is fine" rather than "nobody checked".
    jury_annotations = {
        "calibration_signals": calibration_signals,
        "confidence": calibration.confidence_summary(votes),
    }

    counts = counts_of(votes)

    majority = n // 2 + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top_action, top_n = ranked[0]

    if top_n >= majority:
        decision = Decision(top_action)
        return JuryResult(
            **jury_annotations,
            votes=votes,
            counts=counts,
            decision=decision,
            escalated_to_human=False,
            reason=f"{top_n}/{n} jurors voted {top_action} — decisive majority.",
        )

    # True directional deadlock: BUY and SELL each at exactly half the jury, with no HOLD in the tie
    # (an even jury only). Only this case escalates (see module docstring) — a tie involving HOLD
    # resolves to HOLD below. ``n % 2`` guards odd juries, which cannot split exactly in half.
    if n % 2 == 0 and counts["BUY"] == n // 2 and counts["SELL"] == n // 2:
        return JuryResult(
            **jury_annotations,
            votes=votes,
            counts=counts,
            decision=Decision.ESCALATED,
            escalated_to_human=True,
            reason=(
                f"{counts['BUY']}-{counts['SELL']} BUY/SELL deadlock — escalated to human review. "
                f"A directional tie is never auto-resolved."
            ),
        )

    return JuryResult(
            **jury_annotations,
        votes=votes,
        counts=counts,
        decision=Decision.HOLD,
        escalated_to_human=False,
        reason=f"No action reached a {majority}-vote majority ({_fmt(counts)}) — HOLD, no conviction.",
    )


def _fmt(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}:{v}" for k, v in counts.items())
