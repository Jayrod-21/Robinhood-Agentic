"""Documents that assert facts the code contradicts.

This repo keeps finding one defect: a stored value or a written claim that means something other
than its name says. Prose rots the same way code does and nothing compiles it, so the claims worth
pinning are pinned here.

Scope, deliberately narrow: only claims that are MECHANICALLY checkable against the code or against
another document. Judgement calls — whether a thesis is any good, what the book should hold — are
not testable and are not tested. What is testable is whether a document contradicts itself or the
parser that reads it.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.slate import DEFAULT_SLATE, SLATE_DIR, load_slate, slate_path_for

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"
SLATE = DOCS / DEFAULT_SLATE
THESES = DOCS / "THESES.md"

# "## TSM — Taiwan Semiconductor · ..." — a thesis block's ticker.
_THESIS = re.compile(r"^## ([A-Z][A-Z.]{0,5}) — ", re.M)


def _theses_blocks() -> dict[str, str]:
    """Ticker -> the text of its thesis block."""
    text = THESES.read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    matches = list(_THESIS.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks[m.group(1)] = text[m.start() : end]
    return blocks


# ── the slate and the theses must agree about what is owned ───────────────────────────────────


def test_every_unmarked_thesis_is_a_name_the_slate_actually_targets() -> None:
    """A thesis for a name the slate dropped reads as a live recommendation.

    Deleting those blocks would lose the reasoning that rejected them, so they are kept and marked
    'NOT IN THE SLATE'. This is the test that notices when a block is neither in the slate nor
    marked — which is how UNH, CCJ and IONQ sat here for three months looking like positions.
    """
    slate = load_slate(SLATE)
    unmarked_orphans = [
        ticker
        for ticker, block in _theses_blocks().items()
        if ticker not in slate and "NOT IN THE SLATE" not in block
    ]

    assert not unmarked_orphans, (
        f"{unmarked_orphans} have theses but are not in {DEFAULT_SLATE} and are not marked "
        "'NOT IN THE SLATE'. Either add them to the slate or mark the block."
    )


def test_the_marked_blocks_really_are_absent_from_the_slate() -> None:
    """The other direction: a block marked 'NOT IN THE SLATE' while the slate targets it is a
    contradiction between two documents that an operator has no way to resolve."""
    slate = load_slate(SLATE)
    wrongly_marked = [
        ticker
        for ticker, block in _theses_blocks().items()
        if ticker in slate and "NOT IN THE SLATE" in block
    ]

    assert not wrongly_marked, f"{wrongly_marked} are in the slate but marked as excluded"


def test_the_theses_file_says_how_many_held_names_it_covers() -> None:
    """Ten of fifteen holdings have no thesis. That gap is the sell-discipline rule's whole subject,
    so the file has to state it rather than leave it to be counted."""
    text = THESES.read_text(encoding="utf-8")

    assert "no thesis here at all" in text
    assert "seeded basket" in text or "seeded" in text


def test_stale_prices_in_headings_are_labelled_as_dated() -> None:
    """A three-month-old price in a heading reads as a quote. Break: drop the as-of labels."""
    unlabelled = [
        line
        for line in THESES.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ") and "$" in line and "price as of" not in line
    ]

    assert not unlabelled, f"priced headings without an as-of label: {unlabelled}"


# ── the slate must describe the resolution the code actually implements ───────────────────────


def test_the_slate_documents_the_per_account_resolution_it_is_subject_to(tmp_path: Path) -> None:
    """SLATE.md now carries a table saying which file governs which account. This checks the table
    against `slate_path_for` rather than trusting that the two were written on the same day."""
    text = SLATE.read_text(encoding="utf-8")

    assert f"{SLATE_DIR}/account-" in text, "the slate must name the per-account path scheme"
    assert "no documented slate" in text, "and the state where an account has none"

    # The documented order, exercised.
    (tmp_path / SLATE_DIR).mkdir()
    (tmp_path / DEFAULT_SLATE).write_text("| **TSM** | 22 | $1 | r | w |")
    assert slate_path_for(tmp_path, 1) == tmp_path / DEFAULT_SLATE
    assert slate_path_for(tmp_path, 4) is None, "documented: N with no file gets nothing"
    (tmp_path / SLATE_DIR / "account-1.md").write_text("| **TSM** | 22 | $1 | r | w |")
    assert slate_path_for(tmp_path, 1) == tmp_path / SLATE_DIR / "account-1.md", (
        "documented: the numbered file wins for account 1 too"
    )


def test_the_slate_parses_as_the_slate_it_documents() -> None:
    """The most basic rot: an edit that breaks the table the parser reads. A slate that does not
    parse makes /api/reconciliation answer 503, which reads on the page like a broker outage."""
    slate = load_slate(SLATE)

    assert slate, f"{DEFAULT_SLATE} produced no entries — the allocation table stopped parsing"
    assert "CASH" not in slate, "CASH is a cash target, never a position"
    assert abs(sum(e.target_weight_pct for e in slate.values()) - 90.0) < 0.01, (
        "the non-cash targets should sum to 90% against the documented 10% cash floor"
    )


def test_the_slates_directory_explains_the_no_fallback_rule() -> None:
    readme = (DOCS / SLATE_DIR / "README.md").read_text(encoding="utf-8")

    assert "never falls back" in readme
    assert "no documented slate" in readme


# ── the top-level documents must not assert a superseded fact as current ──────────────────────


def test_the_readme_does_not_claim_the_account_is_empty() -> None:
    """It said "0 positions" for a week after fifteen were seeded."""
    note = README_CURRENT_STATE()

    assert "0 positions" not in note
    assert "fifteen seeded positions" in note or "seeded" in note


def README_CURRENT_STATE() -> str:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    start = text.index("> **Current state")
    return text[start : text.index("\n\n", start)]


def test_the_charter_lists_what_it_no_longer_gets_right() -> None:
    """A dated charter is kept, not rewritten — silently editing a signed document loses the thing
    a charter is for. What it must do is say which of its facts have been superseded."""
    charter = (DOCS / "AGENTIC_ROBINHOOD_v1.md").read_text(encoding="utf-8")

    assert "no longer true" in charter
    assert "Alpaca paper" in charter
    assert "FMP" in charter, "it still says market data comes from Robinhood MCP + yfinance"


def test_the_project_file_does_not_call_robinhood_the_only_tradeable_account() -> None:
    """Present tense, and false the moment a second Alpaca account exists."""
    project = (REPO / "PROJECT.md").read_text(encoding="utf-8")
    account_section = project[project.index("## Accounts") :][:1200]

    assert "ONLY account" not in account_section
    assert "GET /api/accounts" in account_section


def test_the_book_size_survives_a_rewrite_of_the_prose_around_it() -> None:
    """It did not, once. `documented_book_value` was scraped out of the sentence "the $100,000
    Agentic account", so adding a per-account header to SLATE.md silently turned it into None and
    the endpoint reported an unknown book size as though the slate had never claimed one.

    Break: delete the `Documented book:` line from SLATE.md. This goes red.
    """
    from app.routers.reconciliation import _slate_meta

    text = SLATE.read_text(encoding="utf-8")
    _dated, book = _slate_meta(text)

    assert book == 100_000.0, "the slate's documented book size must parse"
    assert "Documented book:" in text, "and from a labelled line, not from a prose phrase"

    # The label alone is enough — the old prose form is not required to be present.
    labelled_only = "Documented book: $250,000\n\n## Allocation (as of 2026-06-03)"
    assert _slate_meta(labelled_only) == ("2026-06-03", 250_000.0)


def test_a_slate_written_before_the_label_existed_still_parses() -> None:
    """The prose fallback is kept deliberately: a per-account slate copied from an older revision
    must not silently lose its book size."""
    from app.routers.reconciliation import _slate_meta

    legacy = "The live target portfolio for the $100,000 Agentic account."
    assert _slate_meta(legacy)[1] == 100_000.0


def test_the_slate_does_not_lean_on_the_legacy_prose_pattern() -> None:
    """The labelled line must be what supplies the book size, not an accident.

    Caught live: the sentence added to EXPLAIN the label quoted the old phrase verbatim, so the
    legacy regex kept matching — on a quotation rather than a claim. The fallback looked healthy
    while doing nothing, and would have broken again the next time that paragraph was edited.
    """
    from app.routers.reconciliation import _DOCUMENTED_BOOK, _DOCUMENTED_BOOK_LABELLED

    text = SLATE.read_text(encoding="utf-8")

    assert _DOCUMENTED_BOOK_LABELLED.search(text), "the labelled line must be present"
    assert not _DOCUMENTED_BOOK.search(text), (
        "SLATE.md still contains the legacy prose pattern, so the book size may be parsing from "
        "the fallback by accident rather than from the label"
    )
