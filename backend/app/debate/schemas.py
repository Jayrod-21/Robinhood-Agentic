"""Typed records for the debate engine. These are the on-the-wire and on-disk shapes."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Vote(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Decision(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    ESCALATED = "ESCALATED"  # 5-5 jury tie — never auto-resolved (3a rule)


class JurorVote(BaseModel):
    agent_id: int = Field(ge=1)
    focus_area: str
    vote: Vote
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class JuryResult(BaseModel):
    votes: list[JurorVote]
    counts: dict[str, int]  # {"BUY": n, "SELL": n, "HOLD": n}
    decision: Decision
    escalated_to_human: bool
    reason: str
    # Ways this panel's output should be read with suspicion — a confidence that is constant
    # across ten lenses, a value repeated verbatim by most jurors, uniform certainty. Empty is the
    # healthy case. These ANNOTATE; the decision above is unchanged by them, because the verdict
    # stays a vote count and vote counts are comparable across model families in a way confidence
    # is not. See app/debate/calibration.py.
    calibration_signals: list[str] = Field(default_factory=list)
    # n / mean / stdev / min / max, plus `usable`: False when the numbers are present but carry no
    # information, so a page can decline to draw a confidence bar rather than assert a measurement
    # nothing made.
    confidence: dict = Field(default_factory=dict)


class DebateTurn(BaseModel):
    """One thing one side said, in order.

    The transcript is the record of the argument as it actually happened. Keeping only the final
    bull and bear cases — which is all BullBear ever held — throws away the exchange, and the
    exchange is where a case either survives contact or does not.
    """

    round_no: int
    side: str          # "bull" | "bear"
    kind: str          # "opening" | "rebuttal" | "closing"
    content: str


class BullBear(BaseModel):
    """The opening statements. Kept for the existing readers and the archive format.

    Superseded for display by `turns`, which carries the whole exchange; these two fields are the
    round-1 openings and nothing more.
    """

    bull_case: str
    bear_case: str


class DebateRecord(BaseModel):
    """A full debate, persisted to logs/debates/*.json and summarized to markdown."""

    id: str
    ticker: str
    created_at: str  # ISO-8601 UTC
    question: str
    price: float | None = None
    fundamentals: dict | None = None
    bull_bear: BullBear | None = None
    # The full exchange in order. Empty on records written before rebuttals existed, which is why
    # it is a plain default rather than something a reader can assume is populated.
    turns: list[DebateTurn] = Field(default_factory=list)
    rounds: int = 1
    jury: JuryResult | None = None
    final_decision: Decision | None = None
    position_size_note: str | None = None
    models: dict[str, str] = Field(default_factory=dict)  # {"jury": ..., "synth": ...}
    # {"calls", "input_tokens", "output_tokens"} — what this debate actually spent. Absent on
    # records written before token accounting existed, and on archive-parsed ones, which is why it
    # is optional rather than defaulted to zeros: a zero would read as "this was free".
    usage: dict[str, int] | None = None
    source: str = "engine"  # "engine" (live) or "archive" (parsed from a markdown log)
