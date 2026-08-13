"""Outcome logging: entry thesis → exit → realized P&L → lesson (issue #4).

This service writes the learning loop's substrate into the evaluation schema from migration 004 —
no new tables. The mapping, table by table:

    entry thesis   → knowledge_base_entries (entry_type='thesis')
    exit           → paper_portfolio_positions.{exit_date, exit_price, exit_reason, realized_pnl}
                     — exactly the four columns rh_app holds a column-level UPDATE grant on;
                     the entry half of a lot is immutable to this role by design
    outcome record → knowledge_base_entries (entry_type='outcome'), same transaction as the exit
    lesson         → knowledge_base_entries (entry_type='lesson'; corrections chain via
                     supersedes_id, never edit — the KB is append-only BY GRANTS)

Write discipline (migrations 004/011 enforce this with REVOKEs, so the code is shaped to match):

  * knowledge_base_entries is APPEND-ONLY. There is deliberately no update/delete function here;
    :func:`record_lesson` takes ``supersedes_id`` because a correction is a NEW row.
  * A lot can be exited ONCE. The UPDATE below guards on ``exit_date IS NULL`` and raises loudly
    on zero rows — never a silent no-op (SENIOR_ENGINEER_BAR §7.2: silent blocking is a defect).
  * Money is Decimal end to end (§7.2 P0). realized_pnl is computed IN SQL from the stored entry
    price — numeric arithmetic on the server, rounded to cents there — so the number recorded can
    never disagree with the lot it belongs to.

Everything here acquires connections via ``app.db.connection()``: when the database is absent the
caller sees :exc:`app.db.DbUnavailable` and degrades; nothing in this module hangs or crashes the
dashboard.

The journal half of issue #4 (docs/agentic_journal.md "Lessons Learned") is
:func:`append_journal_lesson`: same lesson, human-readable, appended atomically. The journal file
is NOT mounted into the backend container, so that function takes an explicit path and is meant
for host-side use (the in-session agent); the DB write is the durable record either way.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from app.db import connection

logger = logging.getLogger("agentic.outcomes")

_JOURNAL_LESSONS_HEADING = "## Lessons Learned"


class OutcomeError(RuntimeError):
    """A domain refusal (unknown symbol, missing lot, double exit) — loud, specific, expected."""


@contextmanager
def _integrity_as_outcome_error() -> Iterator[None]:
    """Translate schema refusals (FKs, CHECKs, the 004 triggers) into loud domain errors.

    The schema is the last line of defense and its messages are deliberately specific — pass them
    through rather than summarizing. Anything else (e.g. InsufficientPrivilege from writing a
    revoked column) is a CODE bug and stays a hard error.
    """
    try:
        yield
    except psycopg.errors.IntegrityError as exc:
        detail = str(exc).strip().splitlines()[0]
        raise OutcomeError(f"write refused by the evaluation schema: {detail}") from exc


@dataclass(frozen=True)
class ExitRecord:
    """What the exit UPDATE actually recorded, straight from RETURNING — not re-derived."""

    portfolio_id: int
    security_id: int
    symbol: str
    entry_date: date
    exit_date: date
    shares: Decimal
    entry_price: Decimal
    exit_price: Decimal
    exit_reason: str
    realized_pnl: Decimal
    outcome_entry_id: int  # the knowledge_base_entries row written in the same transaction


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def resolve_security_id(conn: Any, symbol: str) -> int:
    """The live securities row for ``symbol`` (delisted_at IS NULL — 001's identity rule)."""
    row = conn.execute(
        "SELECT id FROM securities WHERE symbol = %s AND delisted_at IS NULL",
        (symbol,),
    ).fetchone()
    if row is None:
        raise OutcomeError(
            f"symbol {symbol!r} has no live row in securities — "
            "load reference data before recording outcomes for it"
        )
    return row[0]


def record_entry_thesis(
    *,
    symbol: str,
    title: str,
    thesis: str,
    portfolio_id: int | None = None,
    debate_id: int | None = None,
    agent_id: int | None = None,
    as_of: datetime | None = None,
) -> int:
    """Append the entry thesis as a knowledge-base 'thesis' row; returns the new entry id.

    ``as_of`` is the point-in-time anchor (what was knowable when the thesis was written) — it
    defaults to now, and a backfilled thesis must pass the honest historical instant instead.
    """
    if not thesis.strip():
        raise OutcomeError("an empty thesis records nothing a future debate can learn from")
    with _integrity_as_outcome_error(), connection() as conn:
        security_id = resolve_security_id(conn, symbol)
        row = conn.execute(
            """
            INSERT INTO knowledge_base_entries
                (entry_type, title, body, security_id, portfolio_id, debate_id, agent_id, as_of)
            VALUES ('thesis', %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (title, thesis, security_id, portfolio_id, debate_id, agent_id, as_of or _utc_now()),
        ).fetchone()
    entry_id = row[0]
    logger.info("thesis recorded: kb=%s symbol=%s", entry_id, symbol)
    return entry_id


def record_exit(
    *,
    portfolio_id: int,
    symbol: str,
    entry_date: date,
    exit_date: date,
    exit_price: Decimal,
    exit_reason: str,
    lesson: str | None = None,
    agent_id: int | None = None,
) -> ExitRecord:
    """Close a lot and write its outcome record — one transaction, append-only-safe.

    Steps (atomic: the pooled connection commits once, on clean exit):
      1. UPDATE the lot's four exit columns, guarded on ``exit_date IS NULL``; realized_pnl is
         computed server-side from the immutable entry columns.
      2. INSERT the 'outcome' knowledge-base row linking portfolio + security with the numbers.
      3. Optionally INSERT a 'lesson' row (or call :func:`record_lesson` later, linked by ids).
    """
    if exit_price <= 0:
        raise OutcomeError(f"exit price must be positive, got {exit_price}")
    if not exit_reason.strip():
        raise OutcomeError(
            "exit_reason is required — an exit with no recorded reason cannot seed a lesson"
        )

    with _integrity_as_outcome_error(), connection() as conn:
        security_id = resolve_security_id(conn, symbol)

        row = conn.execute(
            """
            UPDATE paper_portfolio_positions
               SET exit_date    = %(exit_date)s,
                   exit_price   = %(exit_price)s,
                   exit_reason  = %(exit_reason)s,
                   realized_pnl = round((%(exit_price)s - entry_price) * shares, 2)
             WHERE portfolio_id = %(portfolio_id)s
               AND security_id  = %(security_id)s
               AND entry_date   = %(entry_date)s
               AND exit_date IS NULL
            RETURNING shares, entry_price, realized_pnl
            """,
            {
                "exit_date": exit_date,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "portfolio_id": portfolio_id,
                "security_id": security_id,
                "entry_date": entry_date,
            },
        ).fetchone()

        if row is None:
            # Zero rows has exactly two causes; name the right one instead of guessing (§7.2).
            existing = conn.execute(
                """
                SELECT exit_date FROM paper_portfolio_positions
                 WHERE portfolio_id = %s AND security_id = %s AND entry_date = %s
                """,
                (portfolio_id, security_id, entry_date),
            ).fetchone()
            if existing is None:
                raise OutcomeError(
                    f"no lot for portfolio={portfolio_id} symbol={symbol!r} entry_date={entry_date} — "
                    "nothing to exit"
                )
            raise OutcomeError(
                f"lot portfolio={portfolio_id} symbol={symbol!r} entry_date={entry_date} already "
                f"exited on {existing[0]} — exits are recorded once; corrections are the "
                "migration role's job"
            )

        shares, entry_price, realized_pnl = row
        now = _utc_now()
        pnl_pct = (exit_price - entry_price) / entry_price * 100

        body = (
            f"Exited {symbol}: {shares} sh, entry {entry_price} ({entry_date}) → "
            f"exit {exit_price} ({exit_date}). Realized P&L {realized_pnl} "
            f"({pnl_pct.quantize(Decimal('0.01'))}%). Reason: {exit_reason}"
        )
        outcome_row = conn.execute(
            """
            INSERT INTO knowledge_base_entries
                (entry_type, title, body, lesson, security_id, portfolio_id, agent_id, as_of)
            VALUES ('outcome', %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                f"{symbol} exit {exit_date}: {exit_reason}",
                body,
                lesson,
                security_id,
                portfolio_id,
                agent_id,
                now,
            ),
        ).fetchone()

    record = ExitRecord(
        portfolio_id=portfolio_id,
        security_id=security_id,
        symbol=symbol,
        entry_date=entry_date,
        exit_date=exit_date,
        shares=shares,
        entry_price=entry_price,
        exit_price=exit_price,
        exit_reason=exit_reason,
        realized_pnl=realized_pnl,
        outcome_entry_id=outcome_row[0],
    )
    logger.info(
        "exit recorded: portfolio=%s %s pnl=%s kb=%s",
        portfolio_id,
        symbol,
        realized_pnl,
        record.outcome_entry_id,
    )
    return record


def record_lesson(
    *,
    title: str,
    lesson: str,
    body: str | None = None,
    symbol: str | None = None,
    portfolio_id: int | None = None,
    debate_id: int | None = None,
    agent_id: int | None = None,
    supersedes_id: int | None = None,
    as_of: datetime | None = None,
) -> int:
    """Append a 'lesson' knowledge-base row; returns the new entry id.

    ``supersedes_id`` is the ONLY correction mechanism: the KB is append-only by grants, so a
    wrong lesson is answered with a new row pointing at the old one, never an edit.
    """
    if not lesson.strip():
        raise OutcomeError("an empty lesson is not a lesson")
    with _integrity_as_outcome_error(), connection() as conn:
        security_id = resolve_security_id(conn, symbol) if symbol is not None else None
        row = conn.execute(
            """
            INSERT INTO knowledge_base_entries
                (entry_type, title, body, lesson, security_id, portfolio_id, debate_id, agent_id,
                 supersedes_id, as_of)
            VALUES ('lesson', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                title,
                body or lesson,
                lesson,
                security_id,
                portfolio_id,
                debate_id,
                agent_id,
                supersedes_id,
                as_of or _utc_now(),
            ),
        ).fetchone()
    entry_id = row[0]
    logger.info("lesson recorded: kb=%s title=%r", entry_id, title)
    return entry_id


def recent_entries(
    *,
    entry_type: str | None = None,
    symbol: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Latest knowledge-base entries, newest first — what a debate reads back as track record."""
    clauses = ["1 = 1"]
    params: list[Any] = []
    if entry_type is not None:
        clauses.append("k.entry_type = %s")
        params.append(entry_type)
    if symbol is not None:
        clauses.append("s.symbol = %s")
        params.append(symbol)
    params.append(limit)

    with connection() as conn:
        rows = conn.execute(
            f"""
            SELECT k.id, k.entry_type, k.title, k.body, k.lesson, s.symbol,
                   k.portfolio_id, k.debate_id, k.agent_id, k.supersedes_id, k.as_of
              FROM knowledge_base_entries k
              LEFT JOIN securities s ON s.id = k.security_id
             WHERE {" AND ".join(clauses)}
             ORDER BY k.as_of DESC, k.id DESC
             LIMIT %s
            """,  # clauses are literals above; every value is a bound parameter
            params,
        ).fetchall()

    return [
        {
            "id": r[0],
            "entry_type": r[1],
            "title": r[2],
            "body": r[3],
            "lesson": r[4],
            "symbol": r[5],
            "portfolio_id": r[6],
            "debate_id": r[7],
            "agent_id": r[8],
            "supersedes_id": r[9],
            "as_of": r[10].isoformat(),
        }
        for r in rows
    ]


def append_journal_lesson(journal_path: Path, *, symbol: str, lesson: str, on: date | None = None) -> None:
    """Append a lesson bullet under the journal's "Lessons Learned" heading, atomically.

    Host-side companion to the DB write (the container does not mount docs/). Insertion is under
    the heading when present, at end-of-file otherwise; the rewrite goes through a same-directory
    temp file + ``os.replace`` so a crash can never leave a half-written journal.
    """
    if not lesson.strip():
        raise OutcomeError("an empty lesson is not a lesson")
    if not journal_path.exists():
        raise OutcomeError(
            f"journal not found at {journal_path} — pass the real docs/agentic_journal.md path"
        )

    stamp = (on or _utc_now().date()).isoformat()
    bullet = f"- **{stamp} — {symbol}:** {lesson.strip()}\n"

    text = journal_path.read_text(encoding="utf-8")
    heading_at = text.find(_JOURNAL_LESSONS_HEADING)
    if heading_at == -1:
        new_text = text.rstrip("\n") + f"\n\n{_JOURNAL_LESSONS_HEADING}\n\n{bullet}"
    else:
        # Insert before the next section heading (or EOF), keeping existing bullets above ours.
        next_heading = text.find("\n## ", heading_at + len(_JOURNAL_LESSONS_HEADING))
        insert_at = len(text) if next_heading == -1 else next_heading
        new_text = text[:insert_at].rstrip("\n") + "\n" + bullet + text[insert_at:]

    fd, tmp_name = tempfile.mkstemp(dir=str(journal_path.parent), prefix=".journal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(new_text)
        os.replace(tmp_name, journal_path)
    except BaseException:
        # Failure of the temp-write path must not leave droppings next to the journal.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    logger.info("journal lesson appended: %s %s", stamp, symbol)
