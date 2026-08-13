"""Thin async wrapper around the Anthropic SDK for the debate engine.

Centralizes client construction (one place reads the key), model selection, timeouts, and the two
call shapes the engine needs: free-text (bull/bear cases) and a forced-tool structured vote. The
SDK already retries 429/5xx with backoff; we add a per-call timeout and fail closed where a caller
asks us to.
"""

from __future__ import annotations

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


async def write_case(model: str, system: str, user: str, max_tokens: int = 700) -> str:
    """Free-text completion for a bull/bear researcher. Returns the joined text blocks."""
    client = _client()
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
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
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "cast_vote":
            return dict(block.input)
    raise ValueError("juror response contained no cast_vote tool call")
