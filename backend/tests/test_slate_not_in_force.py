"""A slate can be a document and not a target: retiring one without deleting it.

WHY THIS EXISTS
    The 2026-06-03 allocation debate ran against a $100 Robinhood book. The account of record moved
    to an Alpaca paper book on 2026-08-17, and the fifteen positions in it were placed by
    ``bin/seed_paper_book.py`` — an owner seeding an equal-dollar basket so the marking job had
    something to value, which that script states is "not the agentic loop deciding anything".

    Reconciling the second against the first produced "0 matched · 5 drifted · 3 missing · 10
    undocumented · 2 guardrail breach(es)" at the top of every morning report, for weeks. None of it
    was a portfolio finding; all of it was the arithmetic of comparing two different books. An
    operator who reads OUT OF SYNC every morning stops reading it, which costs more than no alarm —
    the same failure ``slate_path_for`` prevents BETWEEN accounts, arriving instead through time.

    So: a slate carries an in-force status, absence means in force (every existing slate is
    unchanged), and a retired one yields no targets to anybody rather than stale ones to everybody.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.services import reconcile_check
from app.services.slate import load_governing_slate, load_slate, slate_status

TABLE = "| **TSM** | 22 | $22,000 | Compute anchor | Lowest-variance AI exposure |\n"
MARKER = "> **Slate status: NOT IN FORCE** — superseded by the paper book.\n"


def _slate(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "SLATE.md"
    p.write_text(f"# Target Slate — account 1\n\n{body}", encoding="utf-8")
    return p


def test_absent_marker_means_in_force(tmp_path: Path) -> None:
    """The default must be unchanged behaviour. Every slate written before this feature governs."""
    assert slate_status(_slate(tmp_path, TABLE)).in_force is True


def test_marker_retires_the_slate_and_keeps_the_reason(tmp_path: Path) -> None:
    status = slate_status(_slate(tmp_path, MARKER + "\n" + TABLE))
    assert status.in_force is False
    assert status.reason == "superseded by the paper book."


def test_retiring_does_not_delete_the_record(tmp_path: Path) -> None:
    """The table still parses. Retiring a slate must not destroy the debate that produced it."""
    path = _slate(tmp_path, MARKER + "\n" + TABLE)
    assert load_slate(path)["TSM"].target_weight_pct == 22.0


def test_unreadable_is_not_retired(tmp_path: Path) -> None:
    """A file we cannot read is a parser failure, not a deliberate retirement.

    These must not collapse into one state: the first is a 503 an operator has to fix, the second
    is a normal condition an operator chose.
    """
    assert slate_status(tmp_path / "does-not-exist.md").in_force is True


def test_governing_slate_is_empty_when_retired(tmp_path: Path) -> None:
    """The whole point: no consumer receives a retired slate's weights."""
    _slate(tmp_path, MARKER + "\n" + TABLE)
    slate, path, status = load_governing_slate(tmp_path, 1, 1)
    assert slate == {}
    assert path is not None, "the retired document is still named, so an operator can open it"
    assert status.in_force is False


def test_governing_slate_returns_targets_when_in_force(tmp_path: Path) -> None:
    _slate(tmp_path, TABLE)
    slate, _path, status = load_governing_slate(tmp_path, 1, 1)
    assert status.in_force is True
    assert set(slate) == {"TSM"}


def test_governing_slate_empty_when_no_file(tmp_path: Path) -> None:
    slate, path, status = load_governing_slate(tmp_path, 7, 1)
    assert slate == {} and path is None and status.in_force is False


# ── the check and the report ───────────────────────────────────────────────────────────────────

RETIRED_REPORT = {
    "meta": {
        "slate_source": "docs/SLATE.md",
        "slate_documented": True,
        "slate_in_force": False,
        "slate_retired_reason": "superseded by the paper book.",
        "account_id": 1,
    },
    "summary": {"matched": 0, "drifted": 0, "missing": 0, "unexpected": 0},
    "positions": [],
    "checks": [],
    "note": "Account 1 has no slate in force. `docs/SLATE.md` is retained as the written record.",
}


def test_check_treats_a_retired_slate_as_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """No verdict is stored about a comparison that did not happen (024 stores NULLs)."""
    monkeypatch.setattr(
        "app.routers.reconciliation.reconciliation", lambda *a, **k: RETIRED_REPORT,
    )
    result = reconcile_check.run(halt_on_desync=False)
    assert result["checked"] is False
    assert result["by_design"] is True
    assert "no slate in force" in result["reason"]
    assert "in_sync" not in result, "a skipped check must not imply a verdict either way"


def test_retired_report_section_is_not_a_warning() -> None:
    """A deliberate state must not render as a missed control.

    This is the entire value of the change: if 'no slate in force' still prints ⚠ and 'produced
    without checking', the alarm fatigue it exists to end simply continues under a new heading.
    """
    text = "\n".join(reconcile_check.report_section(
        {"checked": False, "by_design": True, "reason": "Account 1 has no slate in force."},
    ))
    assert "no slate in force" in text
    assert "⚠" not in text
    assert "DID NOT RUN" not in text
    assert "without checking" not in text


def test_genuinely_skipped_check_still_warns() -> None:
    """The other half of the same guarantee: a real gap in the controls stays loud."""
    text = "\n".join(reconcile_check.report_section(
        {"checked": False, "reason": "reconciliation could not run: ConnectionError"},
    ))
    assert "⚠" in text and "DID NOT RUN" in text
    assert "without checking" in text


# ── the route ──────────────────────────────────────────────────────────────────────────────────


def test_the_route_reports_retired_without_inventing_a_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape an operator's page receives: named document, zero counts, one explanation.

    Listing fifteen held names as 'unexpected' against an empty target set is technically true and
    practically a lie — it reads as fifteen findings when the finding is one.
    """
    from app.routers import reconciliation as rec

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "SLATE.md").write_text(
        f"# Target Slate — account 1\n\n{MARKER}\n{TABLE}", encoding="utf-8",
    )
    monkeypatch.setattr(rec, "get_settings", lambda: type("S", (), {"docs_dir": docs})())

    body = rec.reconciliation()
    meta = body["meta"]
    assert meta["slate_in_force"] is False
    assert meta["slate_documented"] is True, "the document exists; saying otherwise is a lie"
    assert meta["slate_source"], "an operator must be able to open the slate that stopped governing"
    assert meta["slate_retired_reason"] == "superseded by the paper book."
    assert body["positions"] == [] and body["checks"] == []
    assert body["summary"]["unexpected"] == 0
    assert "no slate in force" in body["note"]


def test_a_retired_slate_is_reported_before_its_table_is_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retired document whose format has since rotted still reports as retired, not as a 503.

    Otherwise the answer an operator gets about a file that stopped mattering weeks ago is a parser
    error, and they go fix the formatting of a document nothing reads.
    """
    from app.routers import reconciliation as rec

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "SLATE.md").write_text(MARKER + "\nthe table is gone entirely\n", encoding="utf-8")
    monkeypatch.setattr(rec, "get_settings", lambda: type("S", (), {"docs_dir": docs})())

    body = rec.reconciliation()
    assert body["meta"]["slate_in_force"] is False


def test_the_repo_slate_explains_itself_if_it_is_retired() -> None:
    """A bare 'NOT IN FORCE' with no reason is how a temporary silence becomes a permanent one.

    Not a test that the live slate IS retired — that is an owner's call to make and unmake. Only
    that if it is, the document says why, so the next reader does not have to reconstruct it.
    """
    repo_slate = Path(__file__).resolve().parents[2] / "docs" / "SLATE.md"
    status = slate_status(repo_slate)
    if not status.in_force:
        assert status.reason, "docs/SLATE.md is marked NOT IN FORCE without giving a reason"
