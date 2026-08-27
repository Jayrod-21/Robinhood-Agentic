"""The live debate engine: fundamentals → bull/bear → 10-agent jury → aggregated decision.

``run_debate`` is an async generator that yields event dicts as the debate progresses, so a router
can forward them straight to the browser as Server-Sent Events and the UI can render the jury
filling in live (the 3a experience). Every juror runs concurrently; a juror that errors twice
defaults to a low-confidence HOLD rather than taking down the whole debate.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from app.config import get_settings
from app.debate import anthropic_client as ac
from app.debate.aggregate import aggregate
from app.debate.prompts import (
    JUROR_PERSPECTIVES,
    SYSTEM_GROUNDING,
    juror_user_prompt,
    rebuttal_prompt,
    researcher_prompt,
)
from app.debate.records import persist_record
from app.debate.schemas import BullBear, DebateRecord, DebateTurn, Decision, JurorVote, JuryResult, Vote
from app.services import settings_store

logger = logging.getLogger("agentic.debate.engine")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_context(ticker: str) -> tuple[float | None, dict | None]:
    """Blocking fundamentals + price fetch (run off the event loop by the caller)."""
    from src.data import fetch_fundamentals_fmp

    fundamentals = fetch_fundamentals_fmp(ticker)
    price = fundamentals.get("price") if fundamentals else None
    return price, fundamentals


async def _run_juror(
    agent_id: int,
    focus_area: str,
    lens: str,
    ticker: str,
    price: float | None,
    fundamentals: dict | None,
    bull: str,
    bear: str,
    model: str,
    sem: asyncio.Semaphore,
    transcript: str | None = None,
) -> JurorVote | None:
    """One juror: a forced-tool vote with a single retry. Returns None when it cannot vote.

    NONE, NOT A DEFAULT HOLD. This used to return `HOLD` with confidence 0.0 and the reasoning
    "Juror failed to respond; defaulted to HOLD." — and aggregate() counted that as a real vote.

    Discovered live on 2026-08-27, when the Anthropic key hit its usage limit: all ten jurors
    failed, all ten "voted" HOLD, and the panel reported `{BUY: 0, SELL: 0, HOLD: 10} -> HOLD,
    decisive majority`. A confident verdict manufactured out of an outage, indistinguishable from
    ten lenses that actually looked. It is the same defect this project keeps finding — an absent
    measurement impersonating a taken one — and it is the most consequential instance of it,
    because the output is a trading decision.

    An abstention is not a HOLD. HOLD is a judgement that the evidence supports doing nothing;
    an abstention is the absence of a judgement, and the two must not be added together.
    """
    prompt = juror_user_prompt(ticker, focus_area, lens, price, fundamentals, bull, bear, transcript)
    async with sem:
        owner = "unknown"
        for attempt in (1, 2):
            try:
                raw, owner = await ac.cast_vote_attributed(model, SYSTEM_GROUNDING, prompt)
                return JurorVote(
                    agent_id=agent_id,
                    focus_area=focus_area,
                    vote=Vote(str(raw["vote"]).upper()),
                    confidence=max(0.0, min(1.0, float(raw.get("confidence", 0.5)))),
                    reasoning=str(raw.get("reasoning", "")).strip() or "(no reasoning returned)",
                )
            except Exception as exc:  # noqa: BLE001 — one bad juror must not sink the jury
                logger.warning("juror %s attempt %s failed: %s", agent_id, attempt, exc)
                if ac._is_usage_limit(exc):
                    # Not transient. Bench this owner's key so the remaining jurors fail over to
                    # the other owner instead of each spending two more attempts proving the same
                    # key is still out of budget — twenty guaranteed failures per debate.
                    ac.mark_exhausted(owner)
                    break
    # ERROR, not warning: a juror that could not be reached is a hole in the panel, and the whole
    # point of the change above is that the hole is now visible rather than voted.
    logger.error("juror %s (%s) abstained after 2 attempts", agent_id, focus_area)
    return None


def _position_size_note(decision: Decision, jury: JuryResult) -> str:
    """Read-only sizing guidance honoring the charter (max ~25%/name, 10-20% cash, exit-first)."""
    if decision == Decision.ESCALATED:
        return "Escalated 5-5 — no size. A human decides this one before any entry."
    if decision == Decision.BUY:
        conf = sum(v.confidence for v in jury.votes if v.vote == Vote.BUY)
        conf = conf / max(1, sum(1 for v in jury.votes if v.vote == Vote.BUY))
        # Scale a starter into the charter's ~25%/name ceiling by buy-side conviction.
        target = round(min(0.25, 0.10 + 0.15 * conf) * 100)
        return (
            f"If entering: starter up to ~{target}% of equity (charter ceiling 25%/name), keep "
            f"10-20% cash, and record the exit/stop BEFORE the entry. Read-only recommendation."
        )
    if decision == Decision.SELL:
        return "Lean exit — trim or close per the name's stop; don't average down. Read-only."
    return "No conviction — HOLD. Don't force a trade. Read-only."


def _format_transcript(turns: list[DebateTurn]) -> str:
    """The exchange as the jury reads it: in order, labelled, with the round visible.

    Plain text rather than JSON because the reader is a model being asked to weigh an argument, and
    a structure it has to parse first is a structure it can misread.
    """
    lines = []
    for t in turns:
        lines.append(f"[Round {t.round_no} · {t.side.upper()} · {t.kind}]\n{t.content}")
    return "\n\n".join(lines)


async def run_debate(ticker: str, question: str | None = None):
    """Async generator of debate events. Yields dicts with a 'type' field."""
    settings = get_settings()
    # Everything this debate spends is counted here and attached to the record. Per-task, so the
    # cycle job's concurrent debates each get their own tally.
    usage = ac.begin_usage()
    debate_id = f"dbt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    question = question or f"Should the Agentic account hold {ticker}?"
    record = DebateRecord(
        id=debate_id,
        ticker=ticker,
        created_at=_now(),
        question=question,
        models={"jury": settings.jury_model, "synth": settings.synth_model},
    )

    yield {"type": "debate_start", "id": debate_id, "ticker": ticker, "question": question}

    # 1) Context.
    try:
        price, fundamentals = await asyncio.to_thread(_fetch_context, ticker)
    except Exception as exc:  # noqa: BLE001
        logger.warning("context fetch failed for %s: %s", ticker, exc)
        price, fundamentals = None, None
    record.price, record.fundamentals = price, fundamentals
    yield {"type": "context", "price": price, "fundamentals": fundamentals}

    # 2) Bull + bear concurrently (synth model).
    try:
        bull, bear = await asyncio.gather(
            ac.write_case(settings.synth_model, SYSTEM_GROUNDING, researcher_prompt(ticker, "bull", price, fundamentals)),
            ac.write_case(settings.synth_model, SYSTEM_GROUNDING, researcher_prompt(ticker, "bear", price, fundamentals)),
        )
    except ac.DebateUnavailable as exc:
        # F5: never stream str(exc) — even our own exception messages have leaked config paths
        # before. Full detail goes to the server log; the client gets a fixed generic message.
        logger.error("debate %s unavailable: %s", debate_id, exc)
        yield {"type": "error", "message": "Live debate engine is not configured on the server."}
        return
    except Exception:
        # F5: upstream SDK errors carry the provider's status code and response body — server log
        # only (with traceback), never the client stream.
        logger.exception("researcher stage failed for debate %s", debate_id)
        yield {
            "type": "error",
            "message": "Researcher stage failed upstream. Details are in the server logs.",
        }
        return
    record.bull_bear = BullBear(bull_case=bull, bear_case=bear)
    turns: list[DebateTurn] = [
        DebateTurn(round_no=1, side="bull", kind="opening", content=bull),
        DebateTurn(round_no=1, side="bear", kind="opening", content=bear),
    ]
    record.turns = list(turns)
    yield {"type": "bull_complete", "bull_case": bull}
    yield {"type": "bear_complete", "bear_case": bear}
    yield {"type": "turn", "turn": turns[0].model_dump()}
    yield {"type": "turn", "turn": turns[1].model_dump()}

    # 2b) THE ARGUMENT. Openings are written concurrently because neither side should see the other
    # before committing to a position — that part was right. Everything after it was missing: the
    # two cases were handed straight to the jury without either researcher ever answering the other.
    # That is two monologues and a vote, not a debate, and it cannot surface the thing a debate is
    # for — whether a case survives being contradicted by someone trying to break it.
    #
    # Each rebuttal round is SEQUENTIAL per side, because a rebuttal that has not seen what it is
    # rebutting is just another monologue. The two sides within a round still run concurrently:
    # they are answering the previous round, not each other's current turn.
    rounds = max(1, min(int(settings_store.get_or("debate_rounds", 2)), 4))
    record.rounds = rounds

    latest = {"bull": bull, "bear": bear}
    for round_no in range(2, rounds + 1):
        try:
            bull_reply, bear_reply = await asyncio.gather(
                ac.write_case(settings.synth_model, SYSTEM_GROUNDING,
                              rebuttal_prompt(ticker, "bull", latest["bear"], round_no)),
                ac.write_case(settings.synth_model, SYSTEM_GROUNDING,
                              rebuttal_prompt(ticker, "bear", latest["bull"], round_no)),
            )
        except Exception:
            # A failed rebuttal round is not a failed debate: the openings are already on record and
            # the jury can rule on them. Losing the whole thing over round 2 would throw away work
            # that is already paid for.
            logger.exception("rebuttal round %d failed for debate %s; ruling on what exists",
                             round_no, debate_id)
            yield {"type": "notice",
                   "message": f"Rebuttal round {round_no} failed; the jury will rule on the "
                              f"argument up to that point."}
            break

        latest = {"bull": bull_reply, "bear": bear_reply}
        for side, text in (("bull", bull_reply), ("bear", bear_reply)):
            turn = DebateTurn(round_no=round_no, side=side, kind="rebuttal", content=text)
            turns.append(turn)
            yield {"type": "turn", "turn": turn.model_dump()}
        record.turns = list(turns)

    transcript = _format_transcript(turns)

    # 3) Jury — all jurors concurrent (bounded), streamed as each returns.
    sem = asyncio.Semaphore(settings.debate_max_concurrency)
    # Clamped to the briefs that exist: asking for fifteen jurors when ten perspectives are defined
    # would silently seat ten and report a jury of fifteen. The registry's upper bound is 20 because
    # bounds are about catching a slipped decimal, not about what this engine can currently seat.
    jury_size = int(settings_store.get_or("debate_juror_count", settings.jury_size))
    jury_size = max(1, min(jury_size, len(JUROR_PERSPECTIVES)))
    perspectives = JUROR_PERSPECTIVES[:jury_size]
    tasks = [
        asyncio.create_task(
            _run_juror(aid, focus, lens, ticker, price, fundamentals, bull, bear, settings.jury_model,
                       sem, transcript)
        )
        for (aid, focus, lens) in perspectives
    ]
    votes: list[JurorVote] = []
    abstained = 0
    for coro in asyncio.as_completed(tasks):
        vote = await coro
        if vote is None:
            # An abstention is NOT added to `votes`. aggregate() keys every threshold off
            # len(votes), so a juror that could not be reached reduces the panel rather than
            # padding it with a HOLD nobody cast.
            abstained += 1
            yield {
                "type": "juror_abstained",
                "completed": len(votes),
                "abstained": abstained,
                "total": len(tasks),
            }
            continue
        votes.append(vote)
        yield {"type": "juror_complete", "vote": vote.model_dump(), "completed": len(votes),
               "abstained": abstained, "total": len(tasks)}

    votes.sort(key=lambda v: v.agent_id)
    if abstained:
        logger.error(
            "%d of %d jurors abstained; the verdict below rests on %d lens(es)",
            abstained, len(tasks), len(votes),
        )

    # 4) Aggregate + decision.
    jury = aggregate(votes, jury_size)
    record.jury = jury
    record.final_decision = jury.decision
    record.position_size_note = _position_size_note(jury.decision, jury)
    yield {"type": "aggregate", "jury": jury.model_dump()}
    yield {
        "type": "decision",
        "final_decision": jury.decision.value,
        "position_size_note": record.position_size_note,
        "reason": jury.reason,
    }

    # 5) Persist + done.
    try:
        record.usage = dict(usage)
        await asyncio.to_thread(persist_record, record)
        # And into the relational model, so it can be scored. Best-effort by design: the debate is
        # already paid for and its file record is already written, so a database hiccup must not
        # discard completed work. persist_debate never raises.
        from app.services.debate_store import persist_debate

        # mode="json" so enums become the strings the store expects. Plain model_dump() keeps
        # Vote.HOLD as an enum, which stringifies to "Vote.HOLD", matches no decision, and was
        # dropped — 30 debates a day persisted with zero judgments while the file records (already
        # JSON) looked fine. The backfill tests passed for exactly that reason.
        await asyncio.to_thread(persist_debate, record.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001 — persistence failure shouldn't break the response
        logger.warning("failed to persist debate %s: %s", debate_id, exc)
    yield {"type": "debate_complete", "record": record.model_dump()}
