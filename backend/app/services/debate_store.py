"""Persist a finished debate into the relational model, so it can be scored later.

WHY THIS EXISTS
    The engine wrote logs/debates/<id>.json and nothing else. Calibration reads the `debates` and
    `judgments` TABLES. Those two paths never met: eight debates existed on disk while calibration
    reported "0 debate(s), 0 judgment(s)" and would have kept reporting it forever, however many
    debates ran.

THE MAPPING, AND WHY IT IS THIS ONE
    The schema (migration 004) models a debate as proposals judged by judges. The engine produces a
    bull case, a bear case, and a jury of independent voters. So:

      * the bull and bear researchers become `agent_proposals` — stance buy / sell, carrying the
        written case as its rationale;
      * each juror becomes a `judgments` row — its own agent, its own vote, its own stated
        confidence. That is what makes per-agent calibration possible at all: a single blended
        verdict has no one to hold to account;
      * the synthesised verdict becomes one more judgment, by the `synth` judge.

    `ck_judgments_chosen` requires a buy or sell judgment to name the proposal it chose. That is not
    an obstacle to work around — it is the schema insisting a directional call point at the argument
    it accepted, so "who was right" resolves to a case someone actually made. Buy points at the bull
    proposal, sell at the bear.

FAILING TO PERSIST MUST NOT FAIL THE DEBATE
    The debate has already been paid for by the time this runs. A database hiccup here would throw
    away a completed piece of work whose file record is already on disk, so every call site treats
    this as best-effort and logs loudly. The file remains the primary record; this is the queryable
    projection of it.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from app.db import DbUnavailable, connection
from app.services.freshness import parse_iso_utc

logger = logging.getLogger("agentic.debate.store")

# Confidence arrives 0-100 from the jury and is stored 0-1 (numeric(5,4), ck_*_conf).
_CONF_SCALE = 100.0


def _agent_id(conn, *, agent_key: str, kind: str, model: str | None, display_name: str) -> int:
    """The agents row for this key, created once and reused.

    Version is part of the identity in this schema, and it is pinned at 1 here rather than bumped
    per prompt change: nothing in the engine versions its prompts yet, so incrementing would invent
    a distinction the system cannot actually observe. The prompt hash is stored when known, which is
    what a later version scheme would key on.
    """
    row = conn.execute(
        "SELECT id FROM agents WHERE agent_key = %s AND version = 1", (agent_key,)
    ).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO agents (agent_key, version, kind, display_name, model)"
        " VALUES (%s, 1, %s, %s, %s) RETURNING id",
        (agent_key, kind, display_name, model),
    ).fetchone()[0]


def _agent_key(focus_area: Any, *, fallback: str) -> str:
    """A focus area as a valid agents.agent_key (ck_agents_key: ^[a-z][a-z0-9_]{1,48}$)."""
    text = str(focus_area or "").strip().lower()
    cleaned = "".join(c if (c.isalnum() or c == "_") else "_" for c in text).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        return fallback
    return f"juror_{cleaned}"[:49]


def _security_id(conn, symbol: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM securities WHERE upper(symbol) = upper(%s)", (symbol,)
    ).fetchone()
    return row[0] if row else None


def _decision_of(raw: Any) -> str | None:
    """Map the engine's verdict onto ck_judgments_decision. Unknown verdicts are dropped, not
    coerced: writing 'hold' for something the engine did not say would fabricate a call.

    Accepts an enum as well as a string. It used to str() whatever it was given, so a Vote.HOLD
    arriving from model_dump() became "vote.hold", matched nothing, and was discarded — silently,
    which is what let it run for two days.
    """
    if raw is None or raw == "":
        return None
    value = getattr(raw, "value", raw)          # enum -> its value; str -> itself
    value = str(value).strip().lower()
    return value if value in ("buy", "sell", "hold", "escalate") else None


def persist_debate(record: dict[str, Any]) -> int | None:
    """Write one finished debate. Returns the debates.id, or None when it could not be stored.

    Never raises: see the module docstring. The caller has a completed debate either way.
    """
    try:
        return _persist(record)
    except DbUnavailable as exc:
        logger.warning("debate %s not persisted to the database: %s", record.get("id"), exc)
        return None
    except Exception as exc:  # noqa: BLE001 — a mapping bug must not destroy a paid-for debate
        logger.error("debate %s could not be mapped for storage: %s", record.get("id"), exc)
        return None


def _persist(record: dict[str, Any]) -> int | None:
    ticker = (record.get("ticker") or "").upper()
    models = record.get("models") or {}
    jury = record.get("jury") or {}
    votes = jury.get("votes") or []
    final = record.get("final_decision")
    started = record.get("created_at")

    with connection() as conn, conn.transaction():
        security_id = _security_id(conn, ticker)
        if security_id is None:
            # `securities` is reference data with its own loader; inventing a row here would let a
            # typo become a permanent instrument. Without it the debate cannot be scoped to a
            # ticker, and a slate-scoped row would misfile it.
            logger.warning("debate %s: %s is not in securities — not persisted", record.get("id"), ticker)
            return None

        started_at = _parse_ts(started)
        debate_id = conn.execute(
            "INSERT INTO debates (scope, security_id, question, context_as_of, status,"
            " started_at, completed_at) VALUES ('ticker', %s, %s, %s, 'complete', %s, %s)"
            " RETURNING id",
            (security_id, record.get("question") or f"Should the account hold {ticker}?",
             started_at, started_at, _now()),
        ).fetchone()[0]

        # The two researchers, as the proposals a judgment can point at.
        proposals: dict[str, int] = {}
        bull_bear = record.get("bull_bear") or {}
        for stance, key in (("buy", "bull_case"), ("sell", "bear_case")):
            text = (bull_bear.get(key) or "").strip()
            if not text:
                continue
            side = key.removesuffix("_case")
            agent_id = _agent_id(
                conn, agent_key=f"researcher_{side}", kind="persona",
                model=models.get("synth"), display_name=f"{side.title()} researcher",
            )
            proposals[stance] = conn.execute(
                "INSERT INTO agent_proposals (debate_id, agent_id, stance, rationale)"
                " VALUES (%s, %s, %s, %s) RETURNING id",
                (debate_id, agent_id, stance, text),
            ).fetchone()[0]

        written = 0
        dropped: list[str] = []
        for i, vote in enumerate(votes):
            decision = _decision_of(vote.get("vote"))
            if decision is None:
                # LOUD. A vote that cannot be mapped is a juror's opinion being thrown away, and
                # the previous `continue` made that indistinguishable from a debate with no jury.
                dropped.append(repr(vote.get("vote")))
                continue
            # Keyed on FOCUS AREA, not position in the list. The engine gives each juror a standing
            # brief — valuation, cash_flow, tail_risk — and those are identical across every debate
            # on record. That is what makes "is the valuation juror better calibrated than the
            # sentiment juror?" answerable; an index would key on list order, which means nothing
            # and would silently re-attribute a juror's history the day the order changed.
            key = _agent_key(vote.get("focus_area"), fallback=f"juror_{i + 1:02d}")
            agent_id = _agent_id(
                conn, agent_key=key, kind="judge",
                model=models.get("jury"),
                display_name=(vote.get("focus_area") or f"Juror {i + 1}").replace("_", " ").title(),
            )
            written += _write_judgment(
                conn, debate_id=debate_id, agent_id=agent_id, decision=decision,
                confidence=vote.get("confidence"), rationale=vote.get("reasoning"),
                proposals=proposals,
            )

        if final:
            agent_id = _agent_id(
                conn, agent_key="synth", kind="judge",
                model=models.get("synth"), display_name="Synthesiser",
            )
            # No confidence for the synthesiser: the engine does not state one, and the obvious
            # substitute — the share of jurors who agreed — is consensus strength, not a stated
            # belief about being right. Calibration excludes judgments without a confidence, which
            # is the correct treatment: you cannot grade a claim nobody made.
            written += _write_judgment(
                conn, debate_id=debate_id, agent_id=agent_id,
                decision=_decision_of(final if isinstance(final, str) else final.get("decision")),
                confidence=None, rationale=record.get("position_size_note"),
                proposals=proposals,
            )

        if dropped:
            logger.error(
                "debate %s: %d juror vote(s) could not be mapped to a decision and were NOT "
                "stored: %s. The debate is recorded with fewer judgments than it produced.",
                record.get("id"), len(dropped), ", ".join(sorted(set(dropped))),
            )

    logger.info("debate %s stored as debates.id=%s with %d judgment(s)",
                record.get("id"), debate_id, written)
    return debate_id


def _write_judgment(conn, *, debate_id, agent_id, decision, confidence, rationale, proposals) -> int:
    if decision is None:
        return 0
    chosen = proposals.get(decision) if decision in ("buy", "sell") else None
    if decision in ("buy", "sell") and chosen is None:
        # ck_judgments_chosen would reject this. Recording it as a hold would be worse than
        # dropping it: it would put a call in the record that nobody made.
        logger.warning("dropping a %s judgment with no matching proposal to point at", decision)
        return 0
    conn.execute(
        "INSERT INTO judgments (debate_id, judge_agent_id, decision, chosen_proposal_id,"
        " confidence, rationale) VALUES (%s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (debate_id, judge_agent_id) DO NOTHING",
        (debate_id, agent_id, decision, chosen, _confidence(confidence), rationale),
    )
    return 1


def _confidence(raw: Any) -> float | None:
    """0-100 from the jury → 0-1 for storage. Out-of-range values are dropped rather than clamped:
    a confidence of 130 means the producer is not saying what we think it is."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if 0.0 <= value <= 1.0:
        return value              # already a fraction
    if 1.0 < value <= _CONF_SCALE:
        return value / _CONF_SCALE
    return None


def _parse_ts(raw: Any) -> datetime:
    """The debate's own timestamp, falling back to now when it cannot be read.

    Uses the shared parser so a stamp this project writes is read the same way everywhere. The
    fallback differs from the freshness callers on purpose: those decide whether to TRUST a number
    and default to stale, while this one is choosing when a debate happened and has to write
    something — now is the least wrong answer available at that point.
    """
    if isinstance(raw, datetime):
        return raw
    return parse_iso_utc(raw, field="debate created_at") or _now()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def prompt_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
