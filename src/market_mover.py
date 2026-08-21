"""Parse a Market Mover email into the brief shape the dashboard already reads.

WHY EMAIL AND NOT A FEED
    Market Mover is a separate project that publishes a daily brief. The plan was for it to expose
    a JSON URL this app would poll; that URL does not exist, and the briefs arrive as email. So the
    email IS the feed, and this turns one into the shape backend/app/routers/market_context.py has
    been reading (and finding absent) since it was written.

PARSED FROM TEXT, NOT FROM MARKUP
    The obvious approach is CSS classes: the current template has `mm-title`, `mm-source`,
    `mm-summary`, and selecting on those works beautifully — on the current template. Across the
    real corpus of 79 briefs it parses 64 and silently returns nothing for 15, because the template
    changed around April and the older mails carry no classes at all.

    So this keys on the rendered TEXT instead: every brief in both eras marks each story with
    `#N • Impact: X/10` and closes it with a "read full article" link. That pattern parses 79 of 79,
    and survives the next redesign — which there will be, because there has already been one.

    It also recovers the impact score, which the class-based reading missed entirely.

NOTHING PARSED IS AN ERROR, NOT AN EMPTY BRIEF
    market_context.py draws a hard line between "no brief published" and "a brief exists that we
    could not read", because only one of them is a reason to go looking. A parser that returns zero
    headlines for a mail full of them would erase that distinction, so this raises instead.

THE CONTENT IS UNTRUSTED
    Third-party editorial text rendered to an operator, and read by the assistant. It is stored and
    served as DATA — never interpolated into a prompt, never treated as instructions. That rule
    lives with the consumers; it is restated here because this is where the text enters the system.
"""

from __future__ import annotations

import email
import hashlib
import html as html_lib
import logging
import re
from datetime import datetime, timezone
from email import policy
from pathlib import Path
from typing import Any

logger = logging.getLogger("agentic.market_mover")


class MarketMoverParseError(ValueError):
    """A brief that exists and could not be read. Never returned as an empty brief."""


# Each story opens with "#1 • Impact: 8.8/10". The bullet is a literal in both templates; the
# separator between number and word varies, so it is matched loosely.
_STORY = re.compile(
    r"#\s*(\d+)\s*(?:&bull;|&#8226;|•|·|\||-|–)?\s*Impact:\s*([\d.]+)\s*/\s*10", re.I
)

# Closes a story in both eras: "→ Read full article".
_READ_MORE = re.compile(r"(?:→|->)?\s*Read full article", re.I)

# "Top 3 Market-Moving Stories — April 02, 2026"
_BRIEF_DATE = re.compile(r"Stories\s*[—–-]\s*([A-Z][a-z]+ \d{1,2},? \d{4})")

# A trailing " - Reuters"/" - CNBC" on the headline, immediately followed by the same source
# repeated as its own field. Captured rather than stripped blindly: the source is worth keeping.
# The headline is the ANCHOR TEXT of the story's link, in both templates. That is the structural
# invariant this parser rests on. The class names changed around April, and the flattened prose has
# no reliable boundary between headline, outlet and summary — the current template runs the outlet
# straight into the summary with no delimiter at all. But the story has always been a link.
_ANCHOR = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)

# The outlet sits in its own grey span in both eras; only the class attribute differs, so this
# matches on the colour, which has not changed.
_SOURCE_SPAN = re.compile(r"<span[^>]*color:\s*#888[^>]*>(.*?)</span>", re.S | re.I)

_SUMMARY_P = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)


def _text(fragment: str | None) -> str:
    """Tag-stripped, entity-decoded, whitespace-collapsed text from one HTML fragment."""
    if not fragment:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _visible_text(raw_html: str) -> str:
    """Rendered text, with style/script removed FIRST.

    Without that, a template's CSS lands in the body and every field parses out of a stylesheet —
    the first attempt at this returned 8,000 characters of dark-mode overrides.
    """
    s = re.sub(r"<style[^>]*>.*?</style>", " ", raw_html, flags=re.S | re.I)
    s = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    # Unescape FIRST. &nbsp; decodes to \xa0, so unescaping after the collapse leaves hard spaces
    # scattered through the text — which then defeat every \s-based pattern downstream.
    return re.sub(r"\s+", " ", html_lib.unescape(s)).strip()


# Outlet names for the domains these briefs cite. Only a FALLBACK: the brief carries its own source
# element, and that is authoritative. Old-era links are Google News redirects, so a URL-derived
# outlet would report "Google News" for a Reuters story.
_DOMAIN_SOURCES = {
    "bloomberg.com": "Bloomberg",
    "cnbc.com": "CNBC",
    "reuters.com": "Reuters",
    "wsj.com": "WSJ",
    "ft.com": "Financial Times",
    "nytimes.com": "New York Times",
    "marketwatch.com": "MarketWatch",
    "barrons.com": "Barron's",
    "qz.com": "Quartz",
    "apnews.com": "AP",
    "news.google.com": "Google News",
}


def _source_from_url(url: str | None) -> str | None:
    """The outlet inferred from a link, when the brief did not name one itself."""
    if not url:
        return None
    m = re.match(r"https?://(?:www\.)?([^/]+)", url, re.I)
    if not m:
        return None
    host = m.group(1).lower()
    if host in _DOMAIN_SOURCES:
        return _DOMAIN_SOURCES[host]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def parse_email(path: Path | str) -> dict[str, Any]:
    """One .eml → the brief dict. Raises MarketMoverParseError if it cannot be read."""
    path = Path(path)
    with path.open(encoding="utf-8", errors="replace") as fh:
        msg = email.message_from_file(fh, policy=policy.default)

    body = msg.get_body(preferencelist=("html", "plain"))
    if body is None:
        raise MarketMoverParseError(f"{path.name}: no readable body part")
    if body.get_content_type() != "text/html":
        raise MarketMoverParseError(f"{path.name}: only the HTML part carries the brief structure")

    raw = re.sub(r"<style[^>]*>.*?</style>", " ", body.get_content(), flags=re.S | re.I)
    raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)

    marks = list(_STORY.finditer(raw))
    if not marks:
        raise MarketMoverParseError(
            f"{path.name}: no '#N Impact: X/10' story markers found. The template may have changed "
            f"again — a brief that exists and cannot be read is not the same as a day with no "
            f"stories, and the dashboard draws that distinction."
        )

    sent_at = _sent_at(msg)
    headlines: list[dict[str, Any]] = []
    for i, mark in enumerate(marks):
        block = raw[mark.end(): marks[i + 1].start() if i + 1 < len(marks) else len(raw)]

        link = _ANCHOR.search(block)
        if link is None:
            logger.warning("%s: story #%s has no article link; skipped", path.name, mark.group(1))
            continue
        title = _text(link.group(2))
        if not title:
            logger.warning("%s: story #%s has an empty headline; skipped", path.name, mark.group(1))
            continue

        source_el = _SOURCE_SPAN.search(block)
        summary_el = _SUMMARY_P.search(block)
        source = _text(source_el.group(1)) if source_el else _source_from_url(link.group(1))

        # The old template puts the outlet inside the link text: "HEADLINE - Reuters". Trimmed only
        # when it matches the outlet the brief itself names, so this is a removal of a known
        # duplicate rather than a guess at where the headline ends.
        if source:
            trimmed = re.sub(rf"\s*[-–]\s*{re.escape(source)}\s*$", "", title, flags=re.I).strip()
            if trimmed:
                title = trimmed
        headlines.append({
            # Stable across re-ingests of the same mail, so a re-run updates rather than duplicates.
            "id": hashlib.sha256(f"{path.name}|{title}".encode()).hexdigest()[:16],
            "title": title,
            # From the brief's own source element, not the URL: old-era links are Google News
            # redirects, so the URL would report "Google News" for a Reuters story.
            "source": source,
            "url": link.group(1),
            "published_at": sent_at,
            "summary": _text(summary_el.group(1)) if summary_el else "",
            # Not extracted. The brief names companies in prose without a ticker list, and guessing
            # one from a company name is how a headline gets filed against the wrong holding.
            "tickers": [],
            "sentiment": None,
            "impact": float(mark.group(2)),
        })

    if not headlines:
        raise MarketMoverParseError(f"{path.name}: story markers found but no readable stories")

    text = _visible_text(raw)
    return {
        "generated_at": sent_at,
        "subject": msg.get("subject"),
        "brief_date": _brief_date(text),
        "source_file": path.name,
        "macro_read": _bear_case(text),
        "headlines": headlines,
    }


def _bear_case(text: str) -> str | None:
    """The contrarian block — the brief's own counter-argument, which is the most useful part."""
    # No upper bound on the body. It was capped at 600 characters, and a longer block failed the
    # WHOLE match rather than truncating — so the most useful part of the brief came back as None
    # on exactly the days it had most to say.
    m = re.search(
        r"(?:The\s+)?Bear\s+Case\s*(.+?)(?:Generate|Unsubscribe|View in browser|$)",
        text, re.I | re.S,
    )
    if not m:
        return None
    body = re.sub(r"\s+", " ", m.group(1)).strip()
    return body or None


def _brief_date(text: str) -> str | None:
    m = _BRIEF_DATE.search(text)
    if not m:
        return None
    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(m.group(1), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _sent_at(msg) -> str | None:
    raw = msg.get("date")
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
