"""Parsing a Market Mover brief out of an email.

Two fixtures on purpose: the template changed around April 2026, and a parser that only handles the
current one silently returned nothing for a fifth of the corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.market_mover import MarketMoverParseError, parse_email

FIXTURES = Path(__file__).parent / "fixtures" / "market_mover"
CURRENT = FIXTURES / "current_template.eml"
OLD = FIXTURES / "old_template.eml"


@pytest.mark.parametrize("path", [CURRENT, OLD], ids=["current", "old"])
def test_both_templates_parse(path):
    """The class names changed. Keying on them parsed 64 of 79 briefs and returned nothing for the
    other 15 — the exact 'silently empty' failure the Market page is built to distinguish from a
    quiet news day."""
    brief = parse_email(path)
    assert len(brief["headlines"]) == 3, "every brief carries three stories"
    assert brief["generated_at"], "the sent time is what dates the brief"


@pytest.mark.parametrize("path", [CURRENT, OLD], ids=["current", "old"])
def test_the_headline_is_the_headline_and_nothing_else(path):
    """The failure this cost the most effort. Splitting flattened prose on the first sentence end
    swallowed the outlet AND the opening line of the summary, because headlines carry no
    terminating period — a title came back as '...llion dollars — let that number sink in.'

    The headline is the anchor text of the story's link, which is true in both templates.
    """
    for h in parse_email(path)["headlines"]:
        assert h["title"], "a story with no headline is not a story"
        assert len(h["title"]) < 200, f"the title swallowed the summary: {h['title'][:120]!r}"
        assert not h["title"].endswith("."), "a headline is not a sentence"
        if h["summary"]:
            assert h["summary"] not in h["title"], "the summary leaked into the headline"


@pytest.mark.parametrize("path", [CURRENT, OLD], ids=["current", "old"])
def test_every_story_has_its_own_link(path):
    """Each story links TWICE — headline and 'read full article' — so a document-wide href list is
    double-length and index i lands on the wrong story. The Walmart headline came back carrying an
    oil-prices URL that way, which is worse than no link: a wrong fact rendered as a citation.

    The fix was structural rather than arithmetic: the link is searched for INSIDE each story's own
    block, so there is no shared list to fall out of step with. This asserts the property that
    matters — each story's URL points at that story — rather than the mechanism.
    """
    brief = parse_email(path)
    urls = [h["url"] for h in brief["headlines"]]
    assert all(urls), "every story has a link"
    assert len(set(urls)) == len(urls), "two stories share a URL — the pairing is off by one"

    # And the link belongs to ITS story: the last path segment of a story URL should share
    # vocabulary with its own headline, not with a sibling's.
    import re as _re

    for h in brief["headlines"]:
        # Only checkable on direct links. Old-era URLs are Google News redirects whose path is an
        # opaque base64 blob with no words in it to compare against.
        if "news.google.com" in (h["url"] or ""):
            continue
        slug = _re.sub(r"[^a-z]+", " ", (h["url"] or "").lower().rsplit("/", 1)[-1])
        slug_words = {w for w in slug.split() if len(w) > 4}
        title_words = {w for w in _re.sub(r"[^a-z]+", " ", h["title"].lower()).split() if len(w) > 4}
        if slug_words and title_words:
            assert slug_words & title_words, (
                f"URL {h['url']} does not appear to belong to headline {h['title'][:60]!r}"
            )


def test_the_source_comes_from_the_brief_not_the_url():
    """Old-era links are Google News redirects, so a URL-derived outlet reports 'Google News' for a
    Reuters story. The brief names its own source; that is the one to believe."""
    sources = [h["source"] for h in parse_email(OLD)["headlines"]]
    assert all(sources), "every story names an outlet"
    assert "Google News" not in sources, "the redirect host was used instead of the real outlet"


def test_the_impact_score_is_captured():
    """Present in both eras and dropped entirely by the class-based reading."""
    for h in parse_email(CURRENT)["headlines"]:
        assert 0 < h["impact"] <= 10


def test_tickers_are_left_empty_rather_than_guessed():
    """The brief names companies in prose without a ticker list. Inferring one from a company name
    is how a headline gets filed against the wrong holding, and the Market page filters its
    relevance chips on exactly this field."""
    for h in parse_email(CURRENT)["headlines"]:
        assert h["tickers"] == []


def test_an_unreadable_brief_raises_rather_than_returning_nothing(tmp_path):
    """'No brief published' and 'a brief exists that we could not read' are opposite conclusions.
    market_context.py renders them differently, and only one is a reason to go looking."""
    broken = tmp_path / "broken.eml"
    broken.write_text(
        "From: x@example.com\nSubject: [Market Mover] nothing\n"
        "Content-Type: text/html\n\n<html><body><p>no stories here</p></body></html>"
    )
    with pytest.raises(MarketMoverParseError, match="story markers"):
        parse_email(broken)


def test_the_contrarian_block_is_captured_when_present():
    """The bear case is the most useful part of the brief. It was capped at 600 characters, and a
    longer block failed the WHOLE match rather than truncating — so it came back empty on exactly
    the days it had most to say."""
    assert parse_email(CURRENT)["macro_read"], "the current template carries a Bear Case"


def test_a_brief_predating_the_bear_case_is_simply_without_one():
    """Briefs before mid-May have no contrarian section at all. That is a missing FEATURE, not a
    parse failure, and must not be reported as one."""
    brief = parse_email(OLD)
    assert brief["macro_read"] is None
    assert len(brief["headlines"]) == 3, "the stories still parse"
