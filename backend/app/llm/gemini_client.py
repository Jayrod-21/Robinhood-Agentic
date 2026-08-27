"""Gemini, shaped to the two calls the debate engine makes.

WHY RAW HTTP AND NOT google-genai
    The engine needs exactly two things from a provider: free text (a researcher's case) and a
    schema-constrained vote. That is two POSTs. Pulling in a full SDK to make two request shapes
    adds a dependency tree to an image that already ships fastapi, anthropic, pandas and psycopg,
    for no capability we use. httpx is already a backend dependency.

WHY THINKING IS OFF FOR JURORS
    Measured 2026-08-27 on the real vote schema, same prompt:

        gemini-2.5-flash, thinking on   890 total tokens (708 of them thinking)  -> SELL 0.85
        gemini-2.5-flash, thinking off  222 total tokens                         -> SELL 0.80

    Four times the tokens for the same directional answer. A juror is making a bounded judgement
    through one assigned lens on evidence it was handed — not open-ended reasoning — so the thinking
    budget buys very little here. Researchers, who actually construct an argument, keep it.

THE TRAP THIS AVOIDS
    `thoughtsTokenCount` counts against `maxOutputTokens`. With the 500-token budget the Anthropic
    jurors use, 2.5-flash spent 478 on thinking, hit the cap, and returned `{"vote": "SELL", "` —
    truncated mid-string. A juror whose JSON does not parse is a failed juror, so an Anthropic-sized
    budget would have silently emptied every Gemini seat on the panel.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("agentic.llm.gemini")

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)

# The vote schema, in Gemini's dialect. Same three fields the Anthropic tool enforces, and the same
# anchored confidence language — an unanchored scale produced the constant 0.72 that started all of
# this, and there is no reason to expect a different model to be immune to the same failure.
VOTE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "vote": {"type": "STRING", "enum": ["BUY", "SELL", "HOLD"]},
        "confidence": {
            "type": "NUMBER",
            "description": (
                "How much your lens' evidence actually constrains the answer, 0.0-1.0. "
                "0.50 = genuinely ambivalent or the data is missing. 0.65 = a lean you would not "
                "defend hard. 0.80 = your lens points clearly one way. 0.95 = your lens would have "
                "to be wrong about something basic. Use the full range; do not default to a "
                "comfortable middle value."
            ),
        },
        "reasoning": {"type": "STRING", "description": "1-2 sentences, specific to your lens"},
    },
    "required": ["vote", "confidence", "reasoning"],
}


class GeminiError(RuntimeError):
    """A Gemini call failed. Carries whether it was a quota rejection, which is not transient."""

    def __init__(self, message: str, *, quota: bool = False):
        super().__init__(message)
        self.quota = quota


def _body(system: str, user: str, *, max_tokens: int, schema: dict | None, thinking: bool) -> dict:
    config: dict[str, Any] = {"maxOutputTokens": max_tokens}
    if schema is not None:
        config["responseMimeType"] = "application/json"
        config["responseSchema"] = schema
    if not thinking:
        # Explicit zero, not omission: the default is a thinking budget the model chooses, and on
        # 2.5-flash that default consumed the entire output allowance.
        config["thinkingConfig"] = {"thinkingBudget": 0}
    return {
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": config,
    }


async def _post(model: str, api_key: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # The key goes in a header, not the query string: a URL with a credential in it lands in
        # access logs, proxy logs and exception messages.
        response = await client.post(
            f"{BASE}/{model}:generateContent",
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=body,
        )
    if response.status_code != 200:
        detail = response.text[:300]
        raise GeminiError(
            f"gemini {model} returned {response.status_code}: {detail}",
            # 429 is rate/quota. Treated like Anthropic's usage limit so the caller can bench the
            # key rather than retrying into it.
            quota=response.status_code == 429 or "quota" in detail.lower(),
        )
    return response.json()


def _text_of(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise GeminiError("gemini returned no candidates")
    candidate = candidates[0]
    finish = candidate.get("finishReason")
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if finish == "MAX_TOKENS":
        # Named explicitly. A truncated response is the failure mode that silently emptied the
        # Gemini seats during development, and "invalid JSON" would send the next person hunting
        # the schema rather than the budget.
        raise GeminiError(
            f"gemini hit maxOutputTokens before finishing (thinking tokens count against it); "
            f"got {len(text)} chars"
        )
    if not text:
        raise GeminiError(f"gemini returned an empty response (finishReason={finish})")
    return text


def usage_of(payload: dict) -> tuple[int, int]:
    """(input, output) tokens. Thinking is counted as OUTPUT, because it is billed as output."""
    usage = payload.get("usageMetadata") or {}
    output = int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
    return int(usage.get("promptTokenCount") or 0), output


async def write_case(model: str, api_key: str, system: str, user: str, max_tokens: int = 700):
    """Free-text case for a bull or bear researcher. Returns (text, payload).

    Thinking stays ON here — a researcher is constructing an argument, which is the work thinking
    is for. The budget is raised accordingly, since thinking eats the same allowance.
    """
    payload = await _post(
        model, api_key,
        _body(system, user, max_tokens=max_tokens * 4, schema=None, thinking=True),
    )
    return _text_of(payload).strip(), payload


async def cast_vote(model: str, api_key: str, system: str, user: str, max_tokens: int = 500):
    """Schema-constrained vote. Returns ({"vote", "confidence", "reasoning"}, payload)."""
    payload = await _post(
        model, api_key,
        _body(system, user, max_tokens=max_tokens, schema=VOTE_SCHEMA, thinking=False),
    )
    text = _text_of(payload)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiError(f"gemini vote was not valid JSON: {text[:120]}") from exc
    if not isinstance(parsed, dict) or "vote" not in parsed:
        raise GeminiError(f"gemini vote missing required fields: {text[:120]}")
    return parsed, payload
