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
from app.debate.prompts import JUROR_PERSPECTIVES, SYSTEM_GROUNDING, juror_user_prompt, researcher_prompt
from app.debate.records import persist_record
from app.debate.schemas import BullBear, DebateRecord, Decision, JurorVote, JuryResult, Vote

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
) -> JurorVote:
    """One juror: a forced-tool vote with a single retry, defaulting to HOLD on repeated failure."""
    prompt = juror_user_prompt(ticker, focus_area, lens, price, fundamentals, bull, bear)
    async with sem:
        for attempt in (1, 2):
            try:
                raw = await ac.cast_vote(model, SYSTEM_GROUNDING, prompt)
                return JurorVote(
                    agent_id=agent_id,
                    focus_area=focus_area,
                    vote=Vote(str(raw["vote"]).upper()),
                    confidence=max(0.0, min(1.0, float(raw.get("confidence", 0.5)))),
                    reasoning=str(raw.get("reasoning", "")).strip() or "(no reasoning returned)",
                )
            except Exception as exc:  # noqa: BLE001 — one bad juror must not sink the jury
                logger.warning("juror %s attempt %s failed: %s", agent_id, attempt, exc)
    return JurorVote(
        agent_id=agent_id,
        focus_area=focus_area,
        vote=Vote.HOLD,
        confidence=0.0,
        reasoning="Juror failed to respond; defaulted to HOLD.",
    )


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
    yield {"type": "bull_complete", "bull_case": bull}
    yield {"type": "bear_complete", "bear_case": bear}

    # 3) Jury — all jurors concurrent (bounded), streamed as each returns.
    sem = asyncio.Semaphore(settings.debate_max_concurrency)
    perspectives = JUROR_PERSPECTIVES[: settings.jury_size]
    tasks = [
        asyncio.create_task(
            _run_juror(aid, focus, lens, ticker, price, fundamentals, bull, bear, settings.jury_model, sem)
        )
        for (aid, focus, lens) in perspectives
    ]
    votes: list[JurorVote] = []
    for coro in asyncio.as_completed(tasks):
        vote = await coro
        votes.append(vote)
        yield {"type": "juror_complete", "vote": vote.model_dump(), "completed": len(votes), "total": len(tasks)}

    votes.sort(key=lambda v: v.agent_id)

    # 4) Aggregate + decision.
    jury = aggregate(votes, settings.jury_size)
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

        await asyncio.to_thread(persist_debate, record.model_dump())
    except Exception as exc:  # noqa: BLE001 — persistence failure shouldn't break the response
        logger.warning("failed to persist debate %s: %s", debate_id, exc)
    yield {"type": "debate_complete", "record": record.model_dump()}
