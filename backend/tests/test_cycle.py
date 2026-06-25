"""Cycle report formatting — no network, synthetic inputs."""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.jobs.cycle import _format_report


def _account():
    return SimpleNamespace(
        live_total_value=200.0,
        live_equity_value=160.0,
        cash=40.0,
        total_unrealized_pl=5.0,
        total_unrealized_pl_pct=2.5,
        generated_at="2026-06-16T00:00:00Z",
        stale_prices=False,
    )


def test_report_with_account_and_escalation():
    now = datetime(2026, 6, 16, 20, 0, tzinfo=timezone.utc)
    survivors = [SimpleNamespace(ticker="TSM", composite=0.7)]
    debates = [
        {"ticker": "TSM", "decision": "HOLD", "escalated": False, "reason": None, "error": None},
        {"ticker": "NVDA", "decision": "ESCALATED", "escalated": True, "reason": None, "error": None},
    ]
    md = _format_report("close", now, _account(), survivors, survivors, debates)
    assert "CLOSE" in md
    assert "TSM  score 0.700" in md
    assert "$200.00" in md
    assert "- TSM: **HOLD**" in md
    assert "⚠ ESCALATED" in md
    assert "Escalations needing a human:** NVDA" in md


def test_report_no_snapshot_no_debates():
    now = datetime(2026, 6, 16, 9, 30, tzinfo=timezone.utc)
    md = _format_report("open", now, None, [], [], [])
    assert "No snapshot available" in md
    assert "skipped" in md


def test_report_debate_error_surfaced():
    now = datetime(2026, 6, 16, 20, 0, tzinfo=timezone.utc)
    debates = [{"ticker": "MU", "decision": None, "escalated": False, "reason": None, "error": "boom"}]
    md = _format_report("close", now, _account(), [], [], debates)
    assert "MU: ERROR — boom" in md
