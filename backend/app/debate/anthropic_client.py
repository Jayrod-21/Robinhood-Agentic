"""Thin async wrapper around the Anthropic SDK for the debate engine.

Centralizes client construction (one place reads the key), model selection, timeouts, and the two
call shapes the engine needs: free-text (bull/bear cases) and a forced-tool structured vote. The
SDK already retries 429/5xx with backoff; we add a per-call timeout and fail closed where a caller
asks us to.
"""

from __future__ import annotations

import contextvars
import logging
import time

from app.debate.prompts import CAST_VOTE_TOOL

logger = logging.getLogger("agentic.debate.anthropic")


class DebateUnavailable(RuntimeError):
    """Raised when the debate engine can't run (no API key). Surfaced to the client as 503.

    The message must stay client-safe (no env-var names, no on-disk paths) — callers may forward
    it; the operator-facing remediation detail is logged here instead (F5).
    """


def _client() -> tuple[object, str]:
    """(client, key_owner). The owner travels with the client so spend can be attributed.

    KEY SELECTION IS BALANCE-AWARE, NOT ROUND-ROBIN
        Two people share this bill and it was landing entirely on one of them — roughly $9 against
        $0 when this was written. Alternating keys 50/50 from that point would have frozen the gap
        rather than closed it, so each call goes to whichever owner has spent LESS so far
        (app/llm/keys.select). The gap closes on its own and the split then stays approximately
        even, with no scheduled "your turn" bookkeeping.

    The legacy single-key path still works: with only ANTHROPIC_API_KEY set, there is one candidate
    and selection is a no-op.
    """
    from app.llm import keys, ledger

    candidates = keys.available(keys.ANTHROPIC)
    if not candidates:
        logger.error(
            "debate requested but no Anthropic key is configured — add ANTHROPIC_API_KEY to "
            "backend/.env (or the container environment) to enable the live debate engine"
        )
        raise DebateUnavailable("The live debate engine is not configured on this server.")

    chosen = keys.select(keys.ANTHROPIC, ledger.spend_by_owner(keys.ANTHROPIC)) or candidates[0]
    if _exhausted(chosen.owner):
        # This owner's key is known to be out of budget. Prefer any other configured owner over
        # spending two attempts discovering that again — which is what happened on 2026-08-27:
        # Jared's key hit its usage limit, both owners sat at $0 so selection kept choosing it by
        # slot order, and all ten jurors failed against a key that could not have worked.
        alternative = next(
            (k for k in candidates if k.owner != chosen.owner and not _exhausted(k.owner)), None
        )
        if alternative is not None:
            logger.warning(
                "%s is out of API budget; using %s instead", chosen.owner, alternative.owner
            )
            chosen = alternative

    from anthropic import AsyncAnthropic

    # Generous per-request timeout; the SDK retries transient errors on its own.
    client = AsyncAnthropic(api_key=chosen.secret, timeout=120.0, max_retries=2)
    return client, chosen.owner


# ── budget exhaustion ─────────────────────────────────────────────────────────────────────────
#
# A usage-limit rejection is not a transient error and must not be retried like one. Anthropic
# answers 400 with "You have reached your specified API usage limits. You will regain access on
# <date>" — the SDK does not retry a 400, but the ENGINE does (two attempts per juror), and with
# ten jurors that is twenty guaranteed failures per debate against a key that cannot work.
#
# Remembered in-process only, deliberately. A restart clears it, which is the right default: the
# limit lifts on a date this code does not know, and a stale "exhausted" flag surviving a redeploy
# would keep an owner's working key benched. Worst case after a restart is one failed call, which
# re-marks it.
_EXHAUSTED: dict[str, float] = {}
_EXHAUSTED_FOR_SECONDS = 3600.0


def _exhausted(owner: str) -> bool:
    until = _EXHAUSTED.get(owner)
    return until is not None and time.monotonic() < until


def mark_exhausted(owner: str) -> None:
    """Bench one owner's key for an hour after it reports a usage limit."""
    _EXHAUSTED[owner] = time.monotonic() + _EXHAUSTED_FOR_SECONDS
    logger.error("%s has reached its API usage limit; benching that key for an hour", owner)


def _is_usage_limit(exc: BaseException) -> bool:
    """Whether this failure means "no budget" rather than "try again".

    Matched on the message because the SDK surfaces it as a generic BadRequestError — there is no
    distinct exception class for it, and a 400 is otherwise a programming error we should NOT be
    failing over on.
    """
    return "usage limit" in str(exc).lower()


# ── token accounting ──────────────────────────────────────────────────────────────────────────
#
# Nothing recorded what a debate cost. That was tolerable while debates were run by hand, one at a
# time, by someone watching. It is not tolerable on a schedule: the cycle job runs a debate per held
# position on a schedule, and "how much is this spending?" had no answer anywhere in the system —
# not in the record, not in the logs, not in the report.
#
# A ContextVar rather than a module global because the cycle runs two debates concurrently
# (asyncio.Semaphore(2)). A shared counter would bill one debate's jury to the other, which is
# worse than no number at all: a plausible figure attached to the wrong thing.
_usage: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "debate_usage", default=None
)


def begin_usage() -> dict[str, int]:
    """Start counting for the current task. Returns the dict that fills up as calls are made.

    No matching "end" call, deliberately. A context manager would have to be entered and exited
    around an async generator that a disconnecting client can abandon mid-stream, so the exit would
    sometimes never run. There is nothing to clean up: a ContextVar lives in the TASK's context and
    dies with it, and a second debate in the same task installs a fresh tally here anyway.
    """
    tally = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    _usage.set(tally)
    return tally


def _record_usage(resp, *, owner: str | None = None, model: str = "", purpose: str = "") -> None:
    """Record one response's usage, twice, for two different questions.

    The ContextVar tally answers "what did THIS debate cost" and dies with the task. The llm_usage
    row answers "what has each OWNER paid" and outlives everything — it is the record two people
    settle up from, so it is written even when no tally is active. An unmetered call is not an
    error, but an unattributed one is a hole in the accounts.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)

    tally = _usage.get()
    if tally is not None:
        tally["calls"] += 1
        tally["input_tokens"] += input_tokens
        tally["output_tokens"] += output_tokens

    if owner:
        from app.llm import keys, ledger

        ledger.record(
            provider=keys.ANTHROPIC,
            key_owner=owner,
            model=model or getattr(resp, "model", "") or "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            purpose=purpose or None,
        )


async def _create_with_failover(purpose: str, model: str, **kwargs):
    """One Messages call, retried on the OTHER owner's key if this one is out of budget.

    WHY THIS IS CENTRAL RATHER THAN PER-CALL-SITE
        Found live on 2026-08-27. Jared's key served a debate's first two calls, which put him
        ahead in the ledger, so the balance-aware selector correctly routed the next call to Joe —
        whose key was at its usage limit. Cost-splitting worked exactly as designed and drove
        straight into a dead key, and every juror failed against it.

        The failover has to sit here because a usage limit can be discovered on ANY call — the
        bull's opening case, a rebuttal, a juror's vote — and the first draft only handled the
        juror path, so a debate died on the researcher call before the jury was ever assembled.

    ONE retry, on a DIFFERENT owner. Not a loop: with two keys, if the second is also exhausted
    there is nothing left to try, and spinning would turn an outage into a slow outage.
    """
    client, owner = _client()
    try:
        resp = await client.messages.create(model=model, **kwargs)
        return resp, owner
    # Broad, then narrowed: anything that is not a budget limit is re-raised unchanged.
    except Exception as exc:
        if not _is_usage_limit(exc):
            raise
        mark_exhausted(owner)

    from app.llm import keys as key_registry

    alternative = next(
        (
            k
            for k in key_registry.available(key_registry.ANTHROPIC)
            if k.owner != owner and not _exhausted(k.owner)
        ),
        None,
    )
    if alternative is None:
        raise DebateUnavailable(
            "Every configured API key has reached its usage limit. The debate engine cannot run "
            "until one resets."
        )

    logger.warning("%s is out of budget for %s; retrying on %s", owner, purpose, alternative.owner)
    from anthropic import AsyncAnthropic

    retry_client = AsyncAnthropic(api_key=alternative.secret, timeout=120.0, max_retries=2)
    resp = await retry_client.messages.create(model=model, **kwargs)
    return resp, alternative.owner


async def write_case(model: str, system: str, user: str, max_tokens: int = 700) -> str:
    """Free-text completion for a bull/bear researcher. Returns the joined text blocks."""
    resp, owner = await _create_with_failover(
        "debate:write_case",
        model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    _record_usage(resp, owner=owner, model=model, purpose="debate:write_case")
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


async def cast_vote(model: str, system: str, user: str, max_tokens: int = 500) -> dict:
    """Forced-tool structured vote. Returns {"vote", "confidence", "reasoning"}.

    Forcing ``tool_choice`` to ``cast_vote`` guarantees a parseable, schema-validated result rather
    than free-form text we'd have to scrape.
    """
    resp, owner = await _create_with_failover(
        "debate:cast_vote",
        model,
        max_tokens=max_tokens,
        system=system,
        tools=[CAST_VOTE_TOOL],
        tool_choice={"type": "tool", "name": "cast_vote"},
        messages=[{"role": "user", "content": user}],
    )
    _record_usage(resp, owner=owner, model=model, purpose="debate:cast_vote")
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "cast_vote":
            return dict(block.input)
    raise ValueError("juror response contained no cast_vote tool call")


async def cast_vote_attributed(model: str, system: str, user: str, max_tokens: int = 500) -> tuple[dict, str]:
    """cast_vote, plus the owner whose key served it.

    The engine needs the owner so that a usage-limit rejection can bench THAT key rather than
    guessing which one was in play — with two owners configured, benching the wrong one would take
    the working key out and leave the exhausted one in.
    """
    resp, owner = await _create_with_failover(
        "debate:cast_vote",
        model,
        max_tokens=max_tokens,
        system=system,
        tools=[CAST_VOTE_TOOL],
        tool_choice={"type": "tool", "name": "cast_vote"},
        messages=[{"role": "user", "content": user}],
    )
    _record_usage(resp, owner=owner, model=model, purpose="debate:cast_vote")
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "cast_vote":
            return block.input, owner
    raise ValueError("juror response contained no cast_vote tool call")


async def converse(
    *,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 1500,
):
    """One turn of a multi-turn conversation, returning the RAW response.

    Unlike write_case and cast_vote, this hands back the whole response rather than extracting a
    value: a tool-use turn has to be inspected for tool_use blocks and appended to the conversation
    verbatim, so anything this function pulled out would have to be reassembled by the caller.

    `tool_choice` is deliberately left unset — the opposite of cast_vote. A forced tool is right
    when the answer must be structured; here the model has to be free to answer in prose without
    calling anything, and forcing a call would make it invent a lookup to satisfy the constraint.
    """
    kwargs: dict = {"max_tokens": max_tokens, "system": system, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    resp, owner = await _create_with_failover("chat:converse", model, **kwargs)
    _record_usage(resp, owner=owner, model=model, purpose="chat:converse")
    return resp
