"""Which slate governs which account — and the fallback that must never happen.

Before this, `reconciliation()` accepted an `account_id` and then read `docs/SLATE.md` regardless.
With one account that is invisible. With the five paper accounts now planned — an agentic debate
book, a Special-Sprinkle-Sauce book, a second debate test, and two ML/algo testing books — it means
every one of them reconciles against the first account's Reframe-Barbell targets.

An ML-testing account would report as catastrophically out of sync with a strategy it was never
meant to follow. That is worse than no reconciliation: an alarm that is always on is one an
operator learns to skim past, and then the real desync arrives looking exactly the same.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.services.slate import DEFAULT_SLATE, SLATE_DIR, slate_path_for

SLATE_BODY = """# Target Slate

| Ticker | % | $ | Role | Why |
|---|---|---|---|---|
| **TSM** | 22 | $22,000 | Anchor | because |
| **CASH** | 78 | $78,000 | Powder | because |
"""


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    (tmp_path / SLATE_DIR).mkdir()
    return tmp_path


# ── the fallback that must not happen ─────────────────────────────────────────────────────────


def test_an_account_with_no_slate_gets_none_not_account_ones(docs: Path) -> None:
    """The whole reason this function exists. Break: return the default path for any account."""
    (docs / DEFAULT_SLATE).write_text(SLATE_BODY)

    assert slate_path_for(docs, 4) is None, (
        "account 4 must not inherit account 1's targets — a slate is a claim about a SPECIFIC book"
    )


def test_account_one_still_reads_the_file_everything_already_names(docs: Path) -> None:
    """docs/SLATE.md is named by the charter, the README, the reconciliation checks and years of
    journal entries. Moving it would be a rename dressed as a feature."""
    (docs / DEFAULT_SLATE).write_text(SLATE_BODY)

    assert slate_path_for(docs, 1) == docs / DEFAULT_SLATE
    assert slate_path_for(docs, None) == docs / DEFAULT_SLATE, "None means the default account"


def test_a_numbered_slate_is_found_for_any_account(docs: Path) -> None:
    (docs / SLATE_DIR / "account-3.md").write_text(SLATE_BODY)

    assert slate_path_for(docs, 3) == docs / SLATE_DIR / "account-3.md"


def test_the_numbered_file_wins_for_account_one_too(docs: Path) -> None:
    """So a book can move onto the numbered scheme without a code change — and so two files can
    never silently disagree about the same account."""
    (docs / DEFAULT_SLATE).write_text(SLATE_BODY)
    (docs / SLATE_DIR / "account-1.md").write_text(SLATE_BODY)

    assert slate_path_for(docs, 1) == docs / SLATE_DIR / "account-1.md"


def test_no_slate_anywhere_is_none_for_every_account(docs: Path) -> None:
    for account_id in (None, 1, 2, 9):
        assert slate_path_for(docs, account_id) is None


def test_a_directory_named_like_a_slate_is_not_mistaken_for_one(docs: Path) -> None:
    """is_file(), not exists(). A directory would sail through and fail later at read time, where
    the error names a path rather than the account it belongs to."""
    (docs / SLATE_DIR / "account-2.md").mkdir()

    assert slate_path_for(docs, 2) is None


def test_a_non_default_default_account_id_is_honoured(docs: Path) -> None:
    """accounts.DEFAULT_ACCOUNT_ID is passed in rather than assumed, so the two cannot drift."""
    (docs / DEFAULT_SLATE).write_text(SLATE_BODY)

    assert slate_path_for(docs, 2, default_account_id=2) == docs / DEFAULT_SLATE
    assert slate_path_for(docs, 1, default_account_id=2) is None


# ── what the endpoint says when there is nothing to reconcile against ─────────────────────────


def test_an_undocumented_account_reports_a_state_not_a_diff(monkeypatch, tmp_path: Path) -> None:
    """Listing every holding as 'unexpected' against an empty target set is technically true and
    practically a lie: it reads as fifteen findings when the finding is one."""
    from app.routers import reconciliation as mod

    monkeypatch.setattr(mod, "slate_path_for", lambda *a, **k: None)
    result = mod.reconciliation(account_id=4)

    assert result["meta"]["slate_documented"] is False
    assert result["meta"]["slate_source"] is None
    assert result["meta"]["account_id"] == 4
    assert result["positions"] == [], "no diff, because there was nothing to diff against"
    assert result["checks"] == []
    assert "account-4.md" in result["meta"]["expected_slate_path"], "it says how to fix it"


def test_the_preflight_treats_an_undocumented_account_as_not_checked(monkeypatch) -> None:
    """024 stores NULLs for a check that did not run. An account with no targets must land there —
    recording in_sync=False would be a verdict about a comparison that never happened."""
    import app.routers.reconciliation as router
    from app.services import reconcile_check

    monkeypatch.setattr(
        router,
        "reconciliation",
        lambda *a, **k: {
            "meta": {"slate_documented": False, "account_id": 4},
            "note": "Account 4 has no documented slate.",
            "positions": [],
            "checks": [],
        },
    )
    result = reconcile_check.run()

    assert result["checked"] is False
    assert "no documented slate" in result["reason"]
    assert "in_sync" not in result


def test_an_undocumented_account_reads_as_did_not_run_in_the_report(monkeypatch) -> None:
    from app.services import reconcile_check

    section = "\n".join(
        reconcile_check.report_section({"checked": False, "reason": "Account 4 has no slate."})
    )

    assert "DID NOT RUN" in section
    assert "Account 4 has no slate." in section
