"""Thin async wrapper around the Anthropic SDK for the debate engine.

Centralizes client construction (one place reads the key), model selection, timeouts, and the two
call shapes the engine needs: free-text (bull/bear cases) and a forced-tool structured vote. The
SDK already retries 429/5xx with backoff; we add a per-call timeout and fail closed where a caller
asks us to.
"""

from __future__ import annotations

import contextvars
import logging

from app.config import get_settings
from app.debate.prompts import CAST_VOTE_TOOL

logger = logging.getLogger("agentic.debate.anthropic")


class DebateUnavailable(RuntimeError):
    """Raised when the debate engine can't run (no API key). Surfaced to the client as 503.

    The message must stay client-safe (no env-var names, no on-disk paths) — callers may forward
    it; the operator-facing remediation detail is logged here instead (F5).
    """


def _client():
    settings = get_settings()
    if not settings.anthropic_api_key:
        logger.error(
            "debate requested but ANTHROPIC_API_KEY is not set — add it to backend/.env "
            "(or the container environment) to enable the live debate engine"
        )
        raise DebateUnavailable("The live debate engine is not configured on this server.")
    from anthropic import AsyncAnthropic

    # Generous per-request timeout; the SDK retries transient errors on its own.
    return AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=120.0, max_retries=2)


# ── token accounting ──────────────────────────────────────────────────────────────────────────
#
# Nothing recorded what a debate cost. That was tolerable while debates were run by hand, one at a
# time, by someone watching. It is not tolerable on a schedule: the cycle job runs a debate per held
# position, twice a day, and "how much is this spending?" had no answer anywhere in the system —
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


def _record_usage(resp) -> None:
    """Add one response's usage to the active scope. Silent when there is no scope — an unmetered
    call is not an error, it just is not being counted."""
    tally = _usage.get()
    if tally is None:
        return
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    tally["calls"] += 1
    tally["input_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
    tally["output_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)


async def write_case(model: str, system: str, user: str, max_tokens: int = 700) -> str:
    """Free-text completion for a bull/bear researcher. Returns the joined text blocks."""
    client = _client()
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    _record_usage(resp)
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


async def cast_vote(model: str, system: str, user: str, max_tokens: int = 500) -> dict:
    """Forced-tool structured vote. Returns {"vote", "confidence", "reasoning"}.

    Forcing ``tool_choice`` to ``cast_vote`` guarantees a parseable, schema-validated result rather
    than free-form text we'd have to scrape.
    """
    client = _client()
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[CAST_VOTE_TOOL],
        tool_choice={"type": "tool", "name": "cast_vote"},
        messages=[{"role": "user", "content": user}],
    )
    _record_usage(resp)
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "cast_vote":
            return dict(block.input)
    raise ValueError("juror response contained no cast_vote tool call")
