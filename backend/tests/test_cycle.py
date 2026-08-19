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


# ── token accounting ──────────────────────────────────────────────────────────────────────────


def test_a_debates_spend_is_recorded_on_its_own_record():
    """Nothing recorded what a debate cost. Tolerable while debates were run by hand; not on a
    schedule that runs one per held position twice a day, where "how much is this spending?" had
    no answer anywhere in the system."""
    from app.debate import anthropic_client as ac

    class _Resp:
        def __init__(self, i, o):
            self.usage = type("U", (), {"input_tokens": i, "output_tokens": o})()

    tally = ac.begin_usage()
    ac._record_usage(_Resp(100, 20))
    ac._record_usage(_Resp(50, 10))
    assert tally == {"calls": 2, "input_tokens": 150, "output_tokens": 30}


def test_concurrent_debates_do_not_bill_each_other():
    """The cycle runs two debates at once. A module-global counter would attribute one debate's
    jury to the other — a plausible number attached to the wrong thing, which is worse than no
    number at all."""
    import asyncio

    from app.debate import anthropic_client as ac

    class _Resp:
        def __init__(self, i):
            self.usage = type("U", (), {"input_tokens": i, "output_tokens": 0})()

    async def one(cost: int) -> dict:
        tally = ac.begin_usage()
        await asyncio.sleep(0)          # force interleaving
        ac._record_usage(_Resp(cost))
        await asyncio.sleep(0)
        return dict(tally)

    async def main():
        return await asyncio.gather(one(100), one(7))

    a, b = asyncio.run(main())
    assert a["input_tokens"] == 100 and b["input_tokens"] == 7


def test_an_unmetered_call_is_not_an_error():
    """A call outside any scope simply is not counted. Raising there would make the client unusable
    anywhere the ledger has not been started."""
    from app.debate import anthropic_client as ac

    class _Resp:
        usage = type("U", (), {"input_tokens": 5, "output_tokens": 1})()

    ac._usage.set(None)
    ac._record_usage(_Resp())   # must not raise
