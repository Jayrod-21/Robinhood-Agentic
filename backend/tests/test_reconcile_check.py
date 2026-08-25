"""The cycle's reconciliation preflight: the control that was a page nobody opened.

/api/reconciliation could answer "does the broker hold what docs/SLATE.md says" from the day issue
#22's first half shipped. Nothing ever asked it. The cycle read the slate, debated positions against
it and wrote a report twice a day for weeks after the account of record changed brokers — while the
live book matched the document on ZERO of eighteen names, with cash at 92.5% against a 10-20% band.

Everything here exists so that cannot repeat quietly.
"""

from __future__ import annotations

import pytest
from app.services import reconcile_check as mod

# The real shape, taken from the live account on 2026-08-25 — the run that made this necessary.
# The numbers here are the measured ones: 0 matched, 5 drifted, 3 missing, 10 undocumented, cash at
# 92.5%, and a book value that is FINE ($99,996 live against a documented $100,000). The positions
# are what is wrong, not the size of the account — a fixture that overstated that would make this
# suite evidence for a claim the live data does not support.
DESYNCED = {
    "meta": {
        "slate_source": "docs/SLATE.md",
        "slate_dated": "2026-06-03",
        "account_value": 99996.27,
        "documented_book_value": 100000.0,
        "live_cash_pct": 92.5,
        "snapshot_stale": False,
        "in_sync": False,
    },
    "summary": {"matched": 0, "drifted": 5, "missing": 3, "unexpected": 10},
    "positions": [
        {"symbol": "TSM", "status": "missing", "target_weight_pct": 22.0},
        {"symbol": "GEV", "status": "missing", "target_weight_pct": 9.0},
        {
            "symbol": "VST",
            "status": "drifted",
            "live_weight_pct": 0.48,
            "target_weight_pct": 15.0,
            "drift_pct": -14.52,
        },
        {"symbol": "SVRA", "status": "unexpected", "live_weight_pct": 0.52},
    ],
    "checks": [
        {"rule": "Max ~25% per name", "status": "pass", "detail": "ok"},
        {
            "rule": "Cash 10-20% band",
            "status": "breach",
            "detail": "cash is 92.5% of account value, outside the 10-20% band",
        },
    ],
}

IN_SYNC = {
    "meta": {"slate_source": "docs/SLATE.md", "slate_dated": "2026-06-03", "in_sync": True},
    "summary": {"matched": 3},
    "positions": [{"symbol": s, "status": "match"} for s in ("TSM", "VST", "NVDA")],
    "checks": [{"rule": "Cash 10-20% band", "status": "pass", "detail": "ok"}],
}


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch):
    """Install a canned reconciliation report and a known halt setting."""

    def _install(report, *, halt: bool = False):
        import app.routers.reconciliation as router

        if isinstance(report, Exception):
            monkeypatch.setattr(
                router, "reconciliation", lambda *a, **k: (_ for _ in ()).throw(report)
            )
        else:
            monkeypatch.setattr(router, "reconciliation", lambda *a, **k: report)
        monkeypatch.setattr(mod, "_halt_setting", lambda: halt)

    return _install


# ── the verdict ───────────────────────────────────────────────────────────────────────────────


def test_a_desynced_book_is_reported_out_of_sync_with_its_counts(lab) -> None:
    lab(DESYNCED)
    result = mod.run()

    assert result["checked"] is True
    assert result["in_sync"] is False
    assert (result["matched"], result["drifted"], result["missing"], result["unexpected"]) == (
        0,
        1,
        2,
        1,
    )
    assert result["breaches"] == 1


def test_a_matching_book_is_reported_in_sync(lab) -> None:
    lab(IN_SYNC)
    result = mod.run()

    assert result["in_sync"] is True
    assert result["matched"] == 3


def test_a_guardrail_breach_alone_is_enough_to_be_out_of_sync(lab) -> None:
    """Break: compute in_sync from position status only. A book holding exactly what the slate says
    while sitting at 92% cash then reports as healthy."""
    report = {
        **IN_SYNC,
        "checks": [{"rule": "Cash 10-20% band", "status": "breach", "detail": "cash is 92.5%"}],
    }
    lab(report)
    result = mod.run()

    assert result["in_sync"] is False
    assert result["breaches"] == 1


def test_the_counts_are_recomputed_not_taken_from_the_report(lab) -> None:
    """The stored counts and the stored in_sync are compared by a CHECK constraint in 024, so they
    must come from the same arithmetic. Trusting meta.in_sync would let the two disagree in the
    database, which is precisely what that constraint exists to make impossible."""
    lying = {**DESYNCED, "meta": {**DESYNCED["meta"], "in_sync": True}}
    lab(lying)
    result = mod.run()

    assert result["in_sync"] is False, "the rows say drifted/missing/unexpected; meta is ignored"


# ── failing safely ────────────────────────────────────────────────────────────────────────────


def test_a_reconciliation_that_cannot_run_does_not_take_the_cycle_with_it(lab) -> None:
    """A cycle that dies on its own preflight is worse than one that runs uninformed and says so."""
    lab(RuntimeError("broker unreachable"))
    result = mod.run()

    assert result["checked"] is False
    assert "broker unreachable" in result["reason"]
    assert "halt" not in result


def test_a_failed_settings_read_does_not_become_a_halt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break: default _halt_setting to True on error. An unrelated database blip then reads as a
    portfolio problem and stops the cycle."""
    from app.services import settings_store

    def _boom(*_a, **_k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(settings_store, "get_or", _boom)
    assert mod._halt_setting() is False


# ── the halt is opt-in, and never silent ──────────────────────────────────────────────────────


def test_desync_does_not_halt_by_default(lab) -> None:
    """A guardrail that silently stops work is how a cycle quietly does nothing for a week."""
    lab(DESYNCED, halt=False)
    assert "halt" not in mod.run()


def test_the_halt_is_returned_not_raised_so_the_verdict_can_be_recorded_first(lab) -> None:
    """The first draft raised here, which meant the one run an operator most wanted explained —
    the one that refused to proceed — was the one that stored nothing about why."""
    lab(DESYNCED, halt=True)
    result = mod.run()

    assert result["halt"], "the halt travels as data, so the caller records before stopping"
    assert result["in_sync"] is False
    assert result["missing"] == 2, "the full verdict is still present alongside the halt"
    assert "cycle_halt_on_desync" in result["halt"], "the message names the switch that caused it"


def test_a_healthy_book_never_halts_even_with_the_switch_on(lab) -> None:
    lab(IN_SYNC, halt=True)
    assert "halt" not in mod.run()


def test_desync_is_logged_at_error_not_warning(lab, caplog) -> None:
    """This lands in logs/cron/ twice a day, and the whole problem is that six weeks of it looked
    exactly like a healthy run."""
    lab(DESYNCED)
    with caplog.at_level("INFO", logger="agentic.reconcile_check"):
        mod.run()

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "an out-of-sync book must not be reported at INFO or WARNING"
    assert "RECONCILIATION FAILED" in errors[0].getMessage()


# ── the report section ────────────────────────────────────────────────────────────────────────


def test_the_report_says_loudly_that_everything_below_it_is_suspect(lab) -> None:
    lab(DESYNCED)
    section = "\n".join(mod.report_section(mod.run()))

    assert "OUT OF SYNC" in section
    assert "Everything below reasons from that document" in section
    assert "TSM" in section and "missing" in section
    assert "SVRA" in section and "undocumented" in section
    assert "cash is 92.5%" in section


def test_a_book_value_off_by_a_multiple_is_called_out_separately(lab) -> None:
    """A different KIND of wrong: positions drift within a book, but an account value off by a
    multiple means the slate describes a different account entirely.

    SYNTHETIC, unlike the fixture above. The live book's value is not off — it is $99,996 against a
    documented $100,000 — so this covers a case the account has not hit, which is the point of
    having it before it does. The slate WAS written for a $100 Robinhood book, so the shape is not
    hypothetical; it just is not what today's numbers show.
    """
    lab({**DESYNCED, "meta": {**DESYNCED["meta"], "documented_book_value": 100.0}})
    section = "\n".join(mod.report_section(mod.run()))

    assert "describes a different book, not a drifted one" in section


def test_the_live_books_value_is_not_flagged_as_a_different_account(lab) -> None:
    """$99,996 against $100,000 is the same book. Break: widen the ratio test, and every run starts
    claiming the slate describes a different account — an alarm that is always on is not an alarm."""
    lab(DESYNCED)
    section = "\n".join(mod.report_section(mod.run()))

    assert "describes a different book" not in section


def test_a_check_that_did_not_run_is_not_rendered_as_a_clean_bill(lab) -> None:
    """Break: return the in-sync section when `checked` is False. 'We never looked' then renders
    identically to 'we looked and it was fine' — the exact failure this whole change is about."""
    lab(RuntimeError("no snapshot"))
    section = "\n".join(mod.report_section(mod.run()))

    assert "DID NOT RUN" in section
    assert "without checking" in section
    assert "in sync" not in section.lower().replace("out of sync", "")


def test_the_in_sync_section_is_short_and_unalarming(lab) -> None:
    lab(IN_SYNC)
    section = "\n".join(mod.report_section(mod.run()))

    assert "in sync" in section
    assert "⚠" not in section


def test_findings_are_capped_so_one_bad_book_cannot_swamp_the_report(lab) -> None:
    many = {
        **DESYNCED,
        "positions": [
            {"symbol": f"SYM{i}", "status": "unexpected", "live_weight_pct": 0.5}
            for i in range(200)
        ],
    }
    lab(many)
    result = mod.run()

    assert result["unexpected"] == 200, "the COUNT is honest"
    assert len(result["findings"]) <= mod._MAX_LISTED + 1, "the LIST is capped"


def test_matched_positions_are_not_stored_as_findings(lab) -> None:
    lab(DESYNCED)
    kinds = {f.get("kind") for f in mod.run()["findings"]}
    assert "match" not in kinds
