"""Pipeline run history (issue #28): file-backed persistence, the /history comparison math, and
the persist-on-completion hook inside the SSE generator.

The store is the interim JSONL file (see the pipeline section of app/debate/records.py). When the
DB-backed version replaces it, these tests should be ported to the table-backed implementations of
persist_pipeline_run / list_pipeline_runs — the behaviors they pin (round-trip, ordering, corrupt
row tolerance, delta math, error-run exclusion) all still apply.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.debate import records
from app.debate.records import PipelineRunRecord, list_pipeline_runs, persist_pipeline_run
from app.routers import pipeline as pipeline_router


@pytest.fixture()
def runs_dir(tmp_path, monkeypatch):
    """Point the pipeline-run store at a temp logs dir for both the records and router modules."""

    class FakeSettings:
        logs_dir = tmp_path
        debates_dir = tmp_path / "debates"
        marks_ttl_seconds = 45

    monkeypatch.setattr(records, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(pipeline_router, "get_settings", lambda: FakeSettings())
    return tmp_path


def _run(ticker="NVDA", created_at="2026-08-13T10:00:00Z", price=100.0, **kw) -> PipelineRunRecord:
    defaults = dict(
        id=f"plr-{created_at}-{ticker}",
        ticker=ticker,
        created_at=created_at,
        debate_id=f"dbt-{ticker}",
        price_at_run=price,
        screen_passed=True,
        screen_composite=0.71,
        screen_reason=None,
        decision="BUY",
        escalated=False,
    )
    defaults.update(kw)
    return PipelineRunRecord(**defaults)


# --- persistence round-trip ------------------------------------------------------------------


def test_persist_and_list_round_trip(runs_dir):
    persist_pipeline_run(_run())
    out = list_pipeline_runs()
    assert len(out) == 1
    row = out[0]
    assert row["ticker"] == "NVDA"
    assert row["price_at_run"] == 100.0
    assert row["debate_id"] == "dbt-NVDA"
    assert row["decision"] == "BUY"
    assert row["screen_passed"] is True


def test_list_is_newest_first(runs_dir):
    persist_pipeline_run(_run(ticker="OLD", created_at="2026-08-01T00:00:00Z"))
    persist_pipeline_run(_run(ticker="NEW", created_at="2026-08-13T00:00:00Z"))
    persist_pipeline_run(_run(ticker="MID", created_at="2026-08-07T00:00:00Z"))
    assert [r["ticker"] for r in list_pipeline_runs()] == ["NEW", "MID", "OLD"]


def test_missing_file_is_empty_history(runs_dir):
    assert list_pipeline_runs() == []


def test_corrupt_line_is_skipped_not_fatal(runs_dir):
    """One bad JSONL line (crash mid-append, hand edit) must not blank the whole history."""
    persist_pipeline_run(_run(ticker="GOOD"))
    path = Path(runs_dir) / "pipeline_runs.jsonl"
    with path.open("a") as fh:
        fh.write('{"this is": not json\n')
        fh.write(json.dumps({"id": "x"}) + "\n")  # valid JSON but missing required fields
    persist_pipeline_run(_run(ticker="ALSO", created_at="2026-08-14T00:00:00Z"))

    out = list_pipeline_runs()
    assert [r["ticker"] for r in out] == ["ALSO", "GOOD"]


def test_list_respects_limit(runs_dir):
    for i in range(5):
        persist_pipeline_run(_run(ticker=f"T{i}", created_at=f"2026-08-{10 + i:02d}T00:00:00Z"))
    out = list_pipeline_runs(limit=2)
    assert [r["ticker"] for r in out] == ["T4", "T3"]  # newest two, not first two


# --- /history comparison math ----------------------------------------------------------------


def _history_with(monkeypatch, runs, marks):
    monkeypatch.setattr(pipeline_router, "list_pipeline_runs", lambda limit: runs)
    monkeypatch.setattr(pipeline_router, "get_marks", lambda symbols, ttl: marks)
    return pipeline_router._build_history()


def test_history_computes_dollar_and_percent_delta(runs_dir, monkeypatch):
    rows = _history_with(
        monkeypatch,
        [_run(ticker="NVDA", price=100.0).model_dump()],
        {"NVDA": 112.5},
    )
    assert rows[0]["current_price"] == 112.5
    assert rows[0]["delta"] == 12.5
    assert rows[0]["delta_pct"] == 12.5
    assert rows[0]["priced"] is True


def test_history_negative_delta(runs_dir, monkeypatch):
    rows = _history_with(
        monkeypatch,
        [_run(ticker="OXY", price=50.0).model_dump()],
        {"OXY": 40.0},
    )
    assert rows[0]["delta"] == -10.0
    assert rows[0]["delta_pct"] == -20.0


def test_history_unpriced_symbol_degrades_to_nulls(runs_dir, monkeypatch):
    """A yfinance miss (get_marks → None) must yield priced=False with null deltas, not a 500."""
    rows = _history_with(
        monkeypatch,
        [_run(ticker="MISS", price=100.0).model_dump()],
        {"MISS": None},
    )
    assert rows[0]["priced"] is False
    assert rows[0]["current_price"] is None
    assert rows[0]["delta"] is None
    assert rows[0]["delta_pct"] is None


def test_history_null_or_zero_entry_price_yields_no_delta(runs_dir, monkeypatch):
    """No (or nonsense) entry price → show the current mark but never a fabricated percent."""
    rows = _history_with(
        monkeypatch,
        [
            _run(ticker="NOPX", price=None).model_dump(),
            _run(ticker="ZERO", price=0.0, created_at="2026-08-12T00:00:00Z").model_dump(),
        ],
        {"NOPX": 55.0, "ZERO": 55.0},
    )
    for row in rows:
        assert row["current_price"] == 55.0
        assert row["delta"] is None
        assert row["delta_pct"] is None


def test_history_endpoint_no_runs(runs_dir):
    """The endpoint itself: empty store → empty list (and no yfinance call is even attempted)."""
    assert asyncio.run(pipeline_router.history()) == []


# --- persist-on-completion inside the SSE generator ------------------------------------------


def _fake_debate_events(record):
    async def fake_run_debate(ticker, question=None):
        yield {"type": "debate_start", "id": record["id"], "ticker": ticker, "question": "q"}
        yield {"type": "context", "price": record.get("price"), "fundamentals": {"price": record.get("price")}}
        yield {"type": "bull_complete", "bull_case": "bull"}
        yield {"type": "bear_complete", "bear_case": "bear"}
        yield {"type": "aggregate", "jury": record.get("jury") or {"escalated_to_human": False}}
        yield {"type": "decision", "final_decision": record.get("final_decision"),
               "position_size_note": "note", "reason": "r"}
        yield {"type": "debate_complete", "record": record}

    return fake_run_debate


def test_completed_pipeline_run_is_persisted(runs_dir, monkeypatch):
    """Issue #28 regression: finishing a pipeline run must leave a history row carrying the
    debate's price-at-run, decision, screen verdict, and a link back to the debate record."""
    debate_record = {
        "id": "dbt-20260813T100000Z-abc123",
        "ticker": "NVDA",
        "created_at": "2026-08-13T10:00:00Z",
        "price": 181.25,
        "final_decision": "BUY",
        "jury": {"escalated_to_human": False},
    }
    monkeypatch.setattr(pipeline_router, "run_debate", _fake_debate_events(debate_record))
    monkeypatch.setattr(
        pipeline_router, "_screen",
        lambda ticker, fundamentals: {"passed": True, "composite": 0.8, "reason": None, "failed_tier": None},
    )

    async def drain():
        return [ev async for ev in pipeline_router._run_pipeline("NVDA")]

    events = asyncio.run(drain())
    assert events[-1]["type"] == "pipeline_complete"

    out = list_pipeline_runs()
    assert len(out) == 1
    row = out[0]
    assert row["ticker"] == "NVDA"
    assert row["price_at_run"] == 181.25
    assert row["decision"] == "BUY"
    assert row["debate_id"] == "dbt-20260813T100000Z-abc123"
    assert row["created_at"] == "2026-08-13T10:00:00Z"
    assert row["screen_passed"] is True
    assert row["screen_composite"] == 0.8
    assert row["escalated"] is False


def test_errored_pipeline_run_is_not_persisted(runs_dir, monkeypatch):
    """An error mid-debate ends the stream with pipeline_error and records nothing — an errored
    run has no decision or comparison to show."""

    async def failing_debate(ticker, question=None):
        yield {"type": "context", "price": 100.0, "fundamentals": {"price": 100.0}}
        yield {"type": "error", "message": "upstream failed"}

    monkeypatch.setattr(pipeline_router, "run_debate", failing_debate)
    monkeypatch.setattr(
        pipeline_router, "_screen",
        lambda ticker, fundamentals: {"passed": True, "composite": 0.8, "reason": None, "failed_tier": None},
    )

    async def drain():
        return [ev async for ev in pipeline_router._run_pipeline("NVDA")]

    events = asyncio.run(drain())
    assert events[-1]["type"] == "pipeline_error"
    assert list_pipeline_runs() == []


def test_persist_failure_does_not_break_the_stream(runs_dir, monkeypatch, caplog):
    """A full disk / bad mount at persist time must not turn a finished run into a stream error —
    the user still gets pipeline_complete; the failure goes to the server log."""
    debate_record = {"id": "dbt-x", "ticker": "NVDA", "created_at": "2026-08-13T10:00:00Z",
                     "price": 100.0, "final_decision": "HOLD", "jury": None}
    monkeypatch.setattr(pipeline_router, "run_debate", _fake_debate_events(debate_record))
    monkeypatch.setattr(
        pipeline_router, "_screen",
        lambda ticker, fundamentals: {"passed": False, "reason": "fail", "failed_tier": "t1", "composite": None},
    )

    def boom(record):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline_router, "persist_pipeline_run", boom)

    async def drain():
        return [ev async for ev in pipeline_router._run_pipeline("NVDA")]

    import logging

    with caplog.at_level(logging.WARNING, logger="agentic.routers.pipeline"):
        events = asyncio.run(drain())
    assert events[-1]["type"] == "pipeline_complete"
    assert any("failed to persist pipeline run" in r.getMessage() for r in caplog.records)


def test_build_run_record_normalizes_enum_decision(runs_dir):
    """model_dump() in python mode leaves Decision as an enum member — the persisted row must
    still be the plain string, or the JSONL would carry 'Decision.BUY'."""
    from app.debate.schemas import Decision

    rec = pipeline_router._build_run_record(
        "NVDA",
        {"passed": True, "composite": 0.5, "reason": None, "failed_tier": None},
        {"id": "dbt-x", "created_at": "2026-08-13T10:00:00Z", "price": 10.0,
         "final_decision": Decision.BUY, "jury": {"escalated_to_human": True}},
    )
    assert rec.decision == "BUY"
    assert rec.escalated is True
