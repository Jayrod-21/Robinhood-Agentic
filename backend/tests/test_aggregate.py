"""Jury aggregation rules: majority decides, exact tie escalates, plurality holds."""

from app.debate.aggregate import aggregate
from app.debate.schemas import Decision, JurorVote, Vote


def _votes(*spec: str) -> list[JurorVote]:
    """spec is a list of vote strings; builds one juror per entry."""
    return [
        JurorVote(agent_id=i + 1, focus_area=f"lens{i+1}", vote=Vote(v), confidence=0.8, reasoning="r")
        for i, v in enumerate(spec)
    ]


def test_clear_majority_is_decisive():
    votes = _votes("BUY", "BUY", "BUY", "BUY", "BUY", "BUY", "HOLD", "HOLD", "SELL", "HOLD")
    res = aggregate(votes, jury_size=10)
    assert res.decision == Decision.BUY
    assert not res.escalated_to_human
    assert res.counts["BUY"] == 6


def test_five_five_buy_sell_tie_escalates():
    """A true directional deadlock (BUY and SELL each at jury_size/2) escalates to a human."""
    votes = _votes("BUY", "BUY", "BUY", "BUY", "BUY", "SELL", "SELL", "SELL", "SELL", "SELL")
    res = aggregate(votes, jury_size=10)
    assert res.decision == Decision.ESCALATED
    assert res.escalated_to_human is True


def test_five_five_buy_hold_tie_resolves_to_hold():
    """A 5-5 tie that includes HOLD is NOT a directional deadlock — it resolves to HOLD, not escalate.

    Divergence from 3a's "any 5-5 escalates": half the jury already chose the conservative no-action
    verdict, so for a live account the right outcome is to stand down (HOLD), not page a human.
    Without the fix this case escalated (BUY ranks first, HOLD second, both at N/2).
    """
    votes = _votes("BUY", "BUY", "BUY", "BUY", "BUY", "HOLD", "HOLD", "HOLD", "HOLD", "HOLD")
    res = aggregate(votes, jury_size=10)
    assert res.decision == Decision.HOLD
    assert res.escalated_to_human is False


def test_five_five_sell_hold_tie_resolves_to_hold():
    """The SELL/HOLD mirror of the above — also HOLD, also not escalated."""
    votes = _votes("SELL", "SELL", "SELL", "SELL", "SELL", "HOLD", "HOLD", "HOLD", "HOLD", "HOLD")
    res = aggregate(votes, jury_size=10)
    assert res.decision == Decision.HOLD
    assert res.escalated_to_human is False


def test_plurality_short_of_majority_holds():
    votes = _votes("BUY", "BUY", "BUY", "BUY", "SELL", "SELL", "SELL", "HOLD", "HOLD", "HOLD")
    res = aggregate(votes, jury_size=10)
    assert res.decision == Decision.HOLD
    assert not res.escalated_to_human


def test_unanimous_hold_is_decisive_hold():
    votes = _votes(*(["HOLD"] * 10))
    res = aggregate(votes, jury_size=10)
    assert res.decision == Decision.HOLD
    assert res.counts["HOLD"] == 10


def test_sell_majority_is_decisive():
    votes = _votes("SELL", "SELL", "SELL", "SELL", "SELL", "SELL", "BUY", "BUY", "HOLD", "HOLD")
    res = aggregate(votes, jury_size=10)
    assert res.decision == Decision.SELL
    assert not res.escalated_to_human


def test_odd_jury_cannot_directionally_deadlock():
    """An odd jury (9) cannot split BUY/SELL exactly in half, so it never escalates."""
    votes = _votes("BUY", "BUY", "BUY", "BUY", "SELL", "SELL", "SELL", "SELL", "HOLD")
    res = aggregate(votes, jury_size=9)
    assert res.decision == Decision.HOLD
    assert res.escalated_to_human is False


# ── benchmark coverage ────────────────────────────────────────────────────────────────────────


def test_coverage_is_the_measured_ratio_not_a_yes_or_no():
    """It was `1.0 if bench_by_date else None` — a binary "does at least one SPY bar exist",
    reported as the ratio the contract promises. A benchmark with holes produced a line that
    stopped mid-chart under a banner claiming 100%, and the page's `coverage < 1` warning could
    never fire because the value was never between 0 and 1."""
    from app.routers.performance import _coverage

    curve = [
        {"trade_date": "2026-08-17", "benchmark_cumulative_return": 0.01},
        {"trade_date": "2026-08-18", "benchmark_cumulative_return": None},
        {"trade_date": "2026-08-19", "benchmark_cumulative_return": 0.02},
        {"trade_date": "2026-08-20", "benchmark_cumulative_return": None},
    ]
    out = _coverage(curve)
    assert out["coverage"] == 0.5, "half the sessions have a benchmark point"
    assert "2026-08-18" in out["coverage_note"] and "2026-08-20" in out["coverage_note"], (
        "the note must name the missing span — a bare 0.5 says something is wrong, not where"
    )


def test_full_coverage_reports_one_with_no_warning():
    from app.routers.performance import _coverage

    out = _coverage([{"trade_date": "2026-08-19", "benchmark_cumulative_return": 0.01}])
    assert out["coverage"] == 1.0
    assert out["coverage_note"] is None


def test_no_benchmark_at_all_is_zero_coverage_not_missing_data():
    """Distinct from an empty curve: the portfolio has sessions, the benchmark has none."""
    from app.routers.performance import _coverage

    out = _coverage([
        {"trade_date": "2026-08-19", "benchmark_cumulative_return": None},
        {"trade_date": "2026-08-20", "benchmark_cumulative_return": None},
    ])
    assert out["coverage"] == 0.0
    assert "absent" in out["coverage_note"]


def test_an_empty_curve_has_no_coverage_to_report():
    from app.routers.performance import _coverage

    out = _coverage([])
    assert out["coverage"] is None, "no marks is unknown coverage, not zero coverage"
