"""Read the documented target slate and the theses behind it.

WHAT THESE FILES ARE
    ``docs/SLATE.md`` and ``docs/THESES.md`` are hand-maintained markdown that owners edit. They are
    the written record of what the book is SUPPOSED to be — the other half of every reconciliation,
    position and drift question the dashboard asks.

PARSING PROSE IS A LIABILITY, SO IT FAILS LOUDLY
    A heading gains a dash, a table picks up a column, someone writes "22%" instead of "22", and a
    naive parser silently produces a target of 0 — which then renders as "you are 22 points
    overweight" on a page an owner might act on. Wrong is far worse than missing here.

    So: every parse returns None or omits the row rather than guessing, and
    :func:`slate_health` exists so a caller can assert that the symbols it expected actually
    resolved. A test pins that every slate symbol has a thesis, which is what turns a formatting
    change into a red test instead of a wrong number on a page.

NOT A CACHE
    These files change rarely and are small. They are read per request and parsed fresh, because a
    cached slate that outlives an owner's edit is a stale target presented as current — the same
    class of failure as the three-week-old snapshot.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("agentic.slate")

# | **TSM**  | 22 | $22 | Compute anchor | Lowest-variance way ... |
# Ticker is bolded in the source; the bold markers are part of the format, not decoration, so they
# are matched explicitly rather than stripped afterwards with a guess.
_SLATE_ROW = re.compile(
    r"^\|\s*\*\*(?P<ticker>[A-Z]{1,5})\*\*\s*\|\s*(?P<pct>[0-9]+(?:\.[0-9]+)?)\s*\|"
    r"[^|]*\|\s*(?P<role>[^|]*?)\s*\|\s*(?P<why>[^|]*?)\s*\|\s*$"
)

# ## TSM — Taiwan Semiconductor · $439.86 · **conviction HIGH**
_THESIS_HEADING = re.compile(r"^##\s+(?P<ticker>[A-Z]{1,5})\s+[—-]\s+(?P<rest>.+)$")
_CONVICTION = re.compile(r"conviction\s+(?P<level>[A-Z]+)", re.IGNORECASE)
# **Core thesis (6-18mo):** A structural AI-compute monopoly ...
_CORE_THESIS = re.compile(r"^\*\*Core thesis[^:]*:\*\*\s*(?P<text>.+)$", re.MULTILINE)

# > **Slate status: NOT IN FORCE** — the seeded basket is not an allocation decision.
# A slate can exist as a document and still not govern the book. Parsed rather than inferred: the
# alternative is guessing from a date, and a stale-looking slate that IS in force and a current-
# looking one that is NOT are exactly the two cases a heuristic gets backwards. Absent marker means
# IN FORCE, so every slate written before this existed keeps governing its account unchanged.
_SLATE_STATUS_NOT_IN_FORCE = re.compile(
    r"^>?\s*\*\*Slate status:\s*NOT IN FORCE\*\*\s*(?:[—-]\s*(?P<reason>.+?))?\s*$",
    re.MULTILINE,
)

# Sizing discipline, from SLATE.md §Sizing. Read from the file rather than hardcoded so an owner
# editing the document changes the dashboard, which is the entire point of the document.
_STOP_PCT = re.compile(r"stop\s*[−-]\s*(?P<pct>[0-9]+(?:\.[0-9]+)?)\s*%", re.IGNORECASE)
_TRIM_MULT = re.compile(r"past\s*~?\s*(?P<mult>[0-9]+(?:\.[0-9]+)?)\s*[x×]\s*target", re.IGNORECASE)


@dataclass(frozen=True)
class SlateEntry:
    ticker: str
    target_weight_pct: float
    role: str
    size_rationale: str


@dataclass(frozen=True)
class Thesis:
    ticker: str
    headline: str
    conviction: str | None
    body: str
    core: str | None = None
    """The ``**Core thesis (...):**`` sentence — the actual case, which is what a page should show.

    The heading is a title with markdown in it ("Qualcomm · **conviction MED** (asymmetric value)");
    rendering that as the thesis would put formatting characters on screen and say nothing about
    why the position exists. None when the file has no core line for this ticker, which the caller
    must surface rather than paper over with the heading.
    """


@dataclass(frozen=True)
class SizingRules:
    """Defaults match the charter, but the FILE wins when it says otherwise.

    Hardcoding these would mean an owner editing SLATE.md and the dashboard disagreeing about the
    stop — with the dashboard winning silently, which is the wrong way round.
    """

    hard_stop_pct: float = -20.0
    trim_multiple: float = 1.3


# Where a per-account slate lives. Account 1 keeps docs/SLATE.md, because that is the file the
# charter, the README, the reconciliation checks and three years of journal entries all name.
SLATE_DIR = "slates"
DEFAULT_SLATE = "SLATE.md"


def slate_path_for(docs_dir: Path, account_id: int | None, default_account_id: int = 1) -> Path | None:
    """The slate governing one account, or None when that account has no documented slate.

    NONE IS NOT "FALL BACK TO ACCOUNT 1"
        This is the whole point of the function. Before it, reconciliation read docs/SLATE.md no
        matter which account_id it was given, so the moment a second account exists every one of
        them reconciles against the first account's plan. An ML-testing account would report as
        catastrophically out of sync with a strategy it was never meant to follow, and the operator
        would learn to ignore the alarm — which costs more than having no alarm at all.

        A slate is a claim about what a SPECIFIC book should hold. Applying one account's claim to
        another account's holdings does not produce a weaker answer; it produces a wrong one.

    RESOLUTION ORDER
        docs/slates/account-<N>.md          — explicit, for any account
        docs/SLATE.md                       — account 1 only, and only if the above is absent
        None                                — this account has no documented slate

    The per-account file wins even for account 1, so a book can be moved onto the numbered scheme
    without a code change and without two files silently disagreeing about the same account.
    """
    if account_id is None:
        account_id = default_account_id
    explicit = docs_dir / SLATE_DIR / f"account-{int(account_id)}.md"
    if explicit.is_file():
        return explicit
    legacy = docs_dir / DEFAULT_SLATE
    if int(account_id) == default_account_id and legacy.is_file():
        return legacy
    return None


@dataclass(frozen=True)
class SlateStatus:
    """Whether a slate governs its book, and why not when it does not."""

    in_force: bool
    reason: str | None = None


def slate_status(path: Path) -> SlateStatus:
    """Does this slate still govern its account?

    WHY A SLATE CAN BE DOCUMENTED AND NOT IN FORCE
        The 2026-06-03 allocation debate ran against a $100 Robinhood book. The account of record
        moved to an Alpaca paper book on 2026-08-17, and the fifteen positions in it were put there
        by ``bin/seed_paper_book.py`` — an owner seeding an equal-dollar basket so the marking job
        had something to value. That script says so itself: "not the agentic loop deciding
        anything". No allocation debate has run against the current book.

        Reconciling the second against the first produced eighteen findings and two guardrail
        breaches every single morning, at the top of the only report this system writes. Every one
        of them was an artifact of comparing two different books. That is the failure
        :func:`slate_path_for` exists to prevent between accounts, arriving instead through time:
        an operator who reads OUT OF SYNC every day learns to skim past it, and the alarm is then
        worth less than no alarm at all.

    RETIRING IS NOT DELETING
        The document stays, table intact, because it is the written record of a real debate. It
        simply stops being a claim about what the book should hold today.
    """
    text = _read(path)
    if text is None:
        # Unreadable is not "retired". The caller distinguishes these: load_slate() returning empty
        # raises a 503 about a parser failure, which is the correct answer to a file we cannot read.
        return SlateStatus(in_force=True)
    m = _SLATE_STATUS_NOT_IN_FORCE.search(text)
    if m is None:
        return SlateStatus(in_force=True)
    reason = (m.group("reason") or "").strip() or None
    return SlateStatus(in_force=False, reason=reason)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        # Named, not swallowed: a missing slate means every target on the site is absent, and that
        # should be traceable to a file rather than look like "no positions are documented".
        logger.error("cannot read %s: %s", path.name, exc)
        return None


def load_slate(path: Path) -> dict[str, SlateEntry]:
    """Target weights by ticker. CASH is excluded — it is reported in meta, never as a position."""
    text = _read(path)
    if text is None:
        return {}
    entries: dict[str, SlateEntry] = {}
    for line in text.splitlines():
        m = _SLATE_ROW.match(line.strip())
        if not m:
            continue
        ticker = m.group("ticker")
        if ticker == "CASH":
            continue
        entries[ticker] = SlateEntry(
            ticker=ticker,
            target_weight_pct=float(m.group("pct")),
            role=m.group("role").strip(),
            size_rationale=m.group("why").strip(),
        )
    if not entries:
        logger.error(
            "%s parsed to ZERO slate rows — the table format probably changed. Every target on the "
            "site is now absent; this is a parser failure, not an empty slate.", path.name,
        )
    return entries


def load_theses(path: Path) -> dict[str, Thesis]:
    """Theses by ticker, from the ``## TICKER — ...`` headings.

    The body is everything up to the next heading of the same level. Returns nothing for a ticker
    whose heading does not parse, rather than attaching the wrong company's case to a symbol.
    """
    text = _read(path)
    if text is None:
        return {}
    out: dict[str, Thesis] = {}
    current: str | None = None
    headline = ""
    buf: list[str] = []

    def flush() -> None:
        if current:
            body = "\n".join(buf).strip()
            conviction = None
            m = _CONVICTION.search(headline)
            if m:
                conviction = m.group("level").upper()
            core = None
            m_core = _CORE_THESIS.search(body)
            if m_core:
                # Strip emphasis markers only — the words are the thesis and must survive intact.
                core = m_core.group("text").replace("**", "").replace("*", "").strip()
            out[current] = Thesis(
                ticker=current, headline=headline.strip(), conviction=conviction,
                body=body, core=core,
            )

    for line in text.splitlines():
        m = _THESIS_HEADING.match(line.strip())
        if m:
            flush()
            current = m.group("ticker")
            headline = m.group("rest")
            buf = []
            continue
        if line.startswith("## ") and current:
            # A different ## heading ends the current thesis: sections like "Top-down (theme view)"
            # are not a ticker's case and must not be swept into the previous one.
            flush()
            current = None
            buf = []
            continue
        if current:
            buf.append(line)
    flush()
    return out


def load_sizing_rules(path: Path) -> SizingRules:
    """Stop and trim rules, from SLATE.md's sizing section. Falls back to the charter's numbers."""
    text = _read(path)
    if text is None:
        return SizingRules()
    stop = SizingRules.hard_stop_pct
    trim = SizingRules.trim_multiple
    m = _STOP_PCT.search(text)
    if m:
        stop = -abs(float(m.group("pct")))
    else:
        logger.warning("no hard stop found in %s; using the charter default %s%%",
                       path.name, SizingRules.hard_stop_pct)
    m = _TRIM_MULT.search(text)
    if m:
        trim = float(m.group("mult"))
    return SizingRules(hard_stop_pct=stop, trim_multiple=trim)


def load_governing_slate(
    docs_dir: Path, account_id: int | None = None, default_account_id: int = 1,
) -> tuple[dict[str, SlateEntry], Path | None, SlateStatus]:
    """The targets that actually govern an account right now: resolve, then check in-force.

    THE ONE CALL EVERY PAGE SHOULD MAKE
        Both halves are easy to skip, and skipping either produces a confident wrong number rather
        than a missing one. Two routers read ``docs/SLATE.md`` by hand and got both halves wrong:
        they applied account 1's targets to every account, and they would go on presenting a
        retired slate's weights as current. Answering "which targets apply" in one place is what
        keeps the next page from having to remember.

    Returns an EMPTY slate when none is in force — never the retired one's rows. Callers already
    render an absent target as null rather than 0%, which is the behaviour that makes this safe.
    """
    path = slate_path_for(docs_dir, account_id, default_account_id)
    if path is None:
        return {}, None, SlateStatus(in_force=False, reason="no slate on file for this account")
    status = slate_status(path)
    if not status.in_force:
        return {}, path, status
    return load_slate(path), path, status


def slate_health(slate: dict[str, SlateEntry], theses: dict[str, Thesis]) -> dict[str, object]:
    """What parsed, and what did not — so a formatting change is visible rather than silent."""
    missing = sorted(set(slate) - set(theses))
    return {
        "slate_symbols": sorted(slate),
        "thesis_symbols": sorted(theses),
        "slate_without_thesis": missing,
        "parsed_ok": bool(slate),
    }
