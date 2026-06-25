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

    counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for v in votes:
        counts[v.vote.value] += 1

    majority = n // 2 + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top_action, top_n = ranked[0]

    if top_n >= majority:
        decision = Decision(top_action)
        return JuryResult(
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
        votes=votes,
        counts=counts,
        decision=Decision.HOLD,
        escalated_to_human=False,
        reason=f"No action reached a {majority}-vote majority ({_fmt(counts)}) — HOLD, no conviction.",
    )


def _fmt(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}:{v}" for k, v in counts.items())
