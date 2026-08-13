"""Persist debates and read them back — JSON record + markdown summary + JSONL event store.

This realizes the event store sketched in ``logs/README.md``: every finished debate appends a typed
line to ``logs/events.jsonl`` and writes a structured ``logs/debates/<id>.json`` plus a human-readable
``<id>.md``. The two pre-existing hand-written debate narratives are surfaced as read-only "archive"
records so the dashboard's debate list shows the full history, old and new.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from pydantic import BaseModel

from app.config import get_settings
from app.debate.schemas import DebateRecord
from app.validation import is_safe_record_id

logger = logging.getLogger("agentic.debate.records")

# Serializes JSONL event appends. ``persist_record`` runs under ``asyncio.to_thread`` (engine.py), so
# two debates finishing close together append from different threads; a plain text-mode write of
# ``data + "\n"`` is not atomic and can interleave into a corrupt line. The lock guarantees one whole
# line per writer in-process (the guaranteed concurrency mode today).
_events_lock = threading.Lock()


def _safe_record_path(record_id: str, suffix: str) -> Path | None:
    """Resolve ``<debates_dir>/<record_id><suffix>`` only if it stays inside ``debates_dir``.

    Defense-in-depth against path traversal (the ``{record_id}`` URL param flows here):
    1. ``is_safe_record_id`` rejects any id containing ``..``, ``/`` or ``\\`` (a percent-encoded
       ``../`` decodes into ``record_id`` only AFTER Starlette routing, so it must be caught here).
    2. The resolved target must be ``is_relative_to`` the resolved debates dir — so even a novel
       escape (symlink, unexpected normalization) cannot read a file outside the directory.
    Returns the validated path, or None when the id is unsafe (caller treats None as "not found").
    """
    if not is_safe_record_id(record_id):
        return None
    base = get_settings().debates_dir.resolve()
    target = (base / f"{record_id}{suffix}").resolve()
    if not target.is_relative_to(base):
        return None
    return target


def persist_record(record: DebateRecord) -> None:
    """Write the JSON record + markdown summary and append a JSONL event."""
    settings = get_settings()
    settings.debates_dir.mkdir(parents=True, exist_ok=True)

    (settings.debates_dir / f"{record.id}.json").write_text(record.model_dump_json(indent=2))
    (settings.debates_dir / f"{record.id}.md").write_text(_to_markdown(record))

    event = {
        "ts": record.created_at,
        "type": "debate",
        "payload": {
            "id": record.id,
            "ticker": record.ticker,
            "decision": record.final_decision.value if record.final_decision else None,
            "escalated": bool(record.jury and record.jury.escalated_to_human),
        },
    }
    settings.events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event) + "\n"
    # Single locked write of the full line so concurrent appends can't interleave into a corrupt
    # JSONL record that would then trip the reader's broad except.
    with _events_lock, settings.events_path.open("a") as fh:
        fh.write(line)


def list_records() -> list[dict]:
    """Summaries of all debates (engine JSON + archived markdown), newest first."""
    settings = get_settings()
    out: list[dict] = []
    debates_dir = settings.debates_dir
    if not debates_dir.exists():
        return out

    json_stems = set()
    for path in debates_dir.glob("*.json"):
        try:
            rec = DebateRecord.model_validate_json(path.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping unreadable debate record %s: %s", path.name, exc)
            continue
        json_stems.add(path.stem)
        out.append(
            {
                "id": rec.id,
                "ticker": rec.ticker,
                "created_at": rec.created_at,
                "question": rec.question,
                "decision": rec.final_decision.value if rec.final_decision else None,
                "escalated": bool(rec.jury and rec.jury.escalated_to_human),
                "source": rec.source,
            }
        )

    # Archived hand-written narratives: any .md without a matching engine .json.
    for path in debates_dir.glob("*.md"):
        if path.stem in json_stems:
            continue
        out.append(
            {
                "id": path.stem,
                "ticker": None,
                "created_at": _date_from_stem(path.stem),
                "question": _first_heading(path.read_text()),
                "decision": None,
                "escalated": False,
                "source": "archive",
            }
        )

    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out


def get_record(record_id: str) -> dict | None:
    """Full record by id: the structured dict for engine debates, raw markdown for archives.

    ``record_id`` is untrusted (it is the ``{record_id}`` URL path param). Every filesystem access
    goes through ``_safe_record_path``, which rejects traversal ids and confirms containment, so an
    id like ``../../etc/passwd`` (or its ``%2F``-encoded form) returns None instead of reading the
    file. An unsafe id is indistinguishable from a missing record (None → 404) to avoid an
    enumeration oracle.
    """
    json_path = _safe_record_path(record_id, ".json")
    if json_path is None:
        return None  # unsafe id — treat exactly like "not found".

    if json_path.exists():
        try:
            return DebateRecord.model_validate_json(json_path.read_text()).model_dump()
        except Exception as exc:  # noqa: BLE001
            logger.warning("unreadable debate record %s: %s", record_id, exc)
            return None

    md_path = _safe_record_path(record_id, ".md")
    if md_path is not None and md_path.exists():
        return {"id": record_id, "source": "archive", "markdown": md_path.read_text()}
    return None


# --- pipeline run history (issue #28) -------------------------------------------------------
#
# DELIBERATELY FILE-BACKED (interim). The DB-native home for this data is the evaluation tables
# (`agent_proposals` / `paper_portfolios`, which capture price-at-decision), but as of this change
# both are empty and nothing writes them — the DB layer is being wired by a separate workstream.
# Rather than build against tables that don't receive data yet, pipeline runs append to a JSONL
# file next to the debate event store, mirroring the `events.jsonl` pattern above. When the DB
# layer lands, `persist_pipeline_run` / `list_pipeline_runs` are the only two seams to swap: keep
# their signatures, back them with a table, and optionally backfill from the JSONL file.

# Same rationale as `_events_lock`: `persist_pipeline_run` runs under ``asyncio.to_thread``, so two
# pipeline runs finishing close together append from different threads; the lock keeps each JSONL
# line whole. A separate lock from `_events_lock` because the two files are independent.
_pipeline_runs_lock = threading.Lock()


class PipelineRunRecord(BaseModel):
    """One completed pipeline run — the row behind ``GET /api/pipeline/history``.

    Defined here (not ``schemas.py``) because it is owned by this file-backed interim store and
    should move out together with it when the Postgres-backed version replaces this module's part.
    """

    id: str
    ticker: str
    created_at: str  # ISO-8601 UTC
    debate_id: str | None = None  # links to logs/debates/<id>.json → the /debate/[id] detail view
    price_at_run: float | None = None  # entry-side price for the vs-current comparison
    screen_passed: bool | None = None
    screen_composite: float | None = None
    screen_reason: str | None = None
    decision: str | None = None  # BUY / SELL / HOLD / ESCALATED
    escalated: bool = False


def _pipeline_runs_path() -> Path:
    """The JSONL file behind the interim store: ``logs/pipeline_runs.jsonl``.

    Derived from ``logs_dir`` here rather than adding a Settings property — config.py belongs to
    another workstream right now, and this path dies with the file-backed store anyway.
    """
    return get_settings().logs_dir / "pipeline_runs.jsonl"


def persist_pipeline_run(record: PipelineRunRecord) -> None:
    """Append one completed pipeline run to the JSONL history file."""
    path = _pipeline_runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = record.model_dump_json() + "\n"
    # One locked write of the whole line — same interleaving defense as the events log above.
    with _pipeline_runs_lock, path.open("a") as fh:
        fh.write(line)


def list_pipeline_runs(limit: int = 200) -> list[dict]:
    """Pipeline run history, newest first, capped at ``limit``.

    A corrupt line (partial write from a crash, hand-edit) is skipped with a warning rather than
    failing the whole history — one bad record must not blank the page.
    """
    path = _pipeline_runs_path()
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        text = path.read_text()
    except OSError as exc:
        logger.warning("could not read pipeline run history %s: %s", path, exc)
        return []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            out.append(PipelineRunRecord.model_validate_json(line).model_dump())
        except Exception as exc:  # noqa: BLE001 — skip the bad line, keep the history
            logger.warning("skipping unreadable pipeline run (line %d): %s", lineno, exc)
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out[:limit]


# --- helpers --------------------------------------------------------------------------------
def _to_markdown(record: DebateRecord) -> str:
    lines = [f"# Debate — {record.ticker} ({record.id})", "", f"_{record.created_at}_", ""]
    lines.append(f"**Question:** {record.question}")
    if record.price is not None:
        lines.append(f"**Live price:** ${record.price:.2f}")
    if record.final_decision:
        lines.append(f"**Decision:** {record.final_decision.value}")
    if record.jury:
        lines.append(f"**Jury:** {_fmt_counts(record.jury.counts)} — {record.jury.reason}")
    if record.position_size_note:
        lines.append(f"**Sizing:** {record.position_size_note}")
    if record.bull_bear:
        lines += ["", "## Bull", record.bull_bear.bull_case, "", "## Bear", record.bull_bear.bear_case]
    if record.jury:
        lines += ["", "## Jury votes"]
        for v in record.jury.votes:
            lines.append(f"- **{v.agent_id} ({v.focus_area})** → {v.vote.value} "
                         f"(conf {v.confidence:.2f}): {v.reasoning}")
    return "\n".join(lines) + "\n"


def _fmt_counts(counts: dict[str, int]) -> str:
    return " / ".join(f"{k} {v}" for k, v in counts.items())


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("# ").strip()
    return "(untitled debate)"


def _date_from_stem(stem: str) -> str:
    # Filenames like "2026-06-03-debate-1-best-path" → use the leading date.
    parts = stem.split("-")
    if len(parts) >= 3 and parts[0].isdigit():
        return f"{parts[0]}-{parts[1]}-{parts[2]}T00:00:00Z"
    return "1970-01-01T00:00:00Z"
