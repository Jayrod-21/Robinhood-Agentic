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


class BullBear(BaseModel):
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
    jury: JuryResult | None = None
    final_decision: Decision | None = None
    position_size_note: str | None = None
    models: dict[str, str] = Field(default_factory=dict)  # {"jury": ..., "synth": ...}
    # {"calls", "input_tokens", "output_tokens"} — what this debate actually spent. Absent on
    # records written before token accounting existed, and on archive-parsed ones, which is why it
    # is optional rather than defaulted to zeros: a zero would read as "this was free".
    usage: dict[str, int] | None = None
    source: str = "engine"  # "engine" (live) or "archive" (parsed from a markdown log)
