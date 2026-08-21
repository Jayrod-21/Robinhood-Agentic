"""Operator-tunable parameters: the registry, the store, and the fallback.

WHY A REGISTRY RATHER THAN FREE-FORM KEYS
    Every parameter is declared once here with its bounds, its unit, its default, and the sentence
    that says what it does. The API validates against it, the settings page renders from it, and the
    consumers read through it. A key that is not declared cannot be written — a settings table that
    accepts anything is a place for typos to live silently, and a threshold nobody can see is worse
    than a hardcoded one because it looks configured.

BOUNDS ARE GENEROUS, AND THEY ARE NOT THE POINT
    The standing instruction is that guardrails must be tunable, observable, and overridable, never
    a silent block. So bounds exist to catch a slipped decimal (a 150% cash floor, a negative drift
    tolerance), not to enforce a house view. Anything an owner could plausibly mean is allowed.

FALLING BACK IS A FEATURE, AND IT SAYS SO
    Reconciliation had NO database dependency before this. Reading thresholds from Postgres would
    hand it one, so a database outage would take down the page that tells you whether your book
    matches your plan — a page whose inputs (broker + slate file) are both still perfectly readable.

    So a read failure returns the DEFAULTS and reports source='defaults'. The page shows which it
    got. Silently substituting defaults would be the worse bug: a breach evaluated against 1.5 while
    the operator believes they set 3.0 is a guardrail lying about what it enforced.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.db import DbUnavailable, connection

logger = logging.getLogger("agentic.settings")


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    group: str
    unit: str          # "pp" | "%" | "x" | "$B" | "count" | "s"
    default: float
    minimum: float
    maximum: float
    help: str
    used_by: str       # where a change shows up, so the effect is not a guess


# The parameters an operator may tune. Order within a group is reading order.
#
# NOT HERE, deliberately: the hard stop and trim multiple. Those are parsed from docs/SLATE.md
# because the document an owner edits is meant to win; moving them here would let the dashboard
# quietly outrank the written plan. The settings page shows them read-only, pointing at the file.
REGISTRY: tuple[Param, ...] = (
    Param("drift_tolerance_pct", "Drift tolerance", "Guardrails", "pp", 1.5, 0.1, 25.0,
          "How far a holding's weight may sit from its slate target before it counts as drifted "
          "rather than matched.",
          "Reconcile — position status"),
    Param("max_position_pct", "Max position", "Guardrails", "%", 25.0, 1.0, 100.0,
          "The largest share of account value any single name may hold before the rule breaches.",
          "Reconcile — checks"),
    Param("cash_floor_pct", "Cash floor", "Guardrails", "%", 10.0, 0.0, 100.0,
          "The bottom of the cash band. Below this, the book has no dry powder for an air-pocket.",
          "Reconcile — checks"),
    Param("cash_ceiling_pct", "Cash ceiling", "Guardrails", "%", 20.0, 0.0, 100.0,
          "The top of the cash band. Above this, capital is idle rather than deployed.",
          "Reconcile — checks"),
    Param("off_factor_floor_pct", "Off-factor floor", "Guardrails", "%", 20.0, 0.0, 100.0,
          "Minimum combined weight of the genuine decorrelators (V + CVX). The barbell hedges which "
          "end of the AI reroute wins; this is what hedges the cycle rolling over at all.",
          "Reconcile — checks"),
    Param("screen_min_market_cap_b", "Min market cap", "Screen", "$B", 5.0, 0.0, 1000.0,
          "Smallest company the screen will consider.",
          "Scan — tier gates"),
    Param("screen_max_peg", "Max PEG", "Screen", "x", 2.0, 0.1, 20.0,
          "Highest price/earnings-to-growth the screen will pass.",
          "Scan — tier gates"),
    Param("screen_min_fcf_yield_pct", "Min FCF yield", "Screen", "%", 3.0, 0.0, 50.0,
          "Lowest free-cash-flow yield the screen will pass.",
          "Scan — tier gates"),
    Param("screen_piotroski_min", "Min Piotroski", "Screen", "count", 5.0, 0.0, 9.0,
          "Signals out of nine a name must pass. Cary's variant; an incomplete score is judged on "
          "the signals that could be computed.",
          "Scan — tier gates"),
    Param("debate_min_interval_s", "Debate cooldown", "Runs", "s", 60.0, 0.0, 3600.0,
          "Minimum seconds between debate or pipeline runs. Each run spends tokens, so this is the "
          "brake on an accidental double-click costing real money.",
          "Debate & Pipeline — run button"),
    Param("marks_ttl_seconds", "Price refresh", "Runs", "s", 120.0, 15.0, 600.0,
          "How long a live price is reused before the provider is asked again. Every position "
          "costs one call per refresh — this plan has no batch quote — so halving this doubles "
          "the call rate.",
          "Portfolio, Reconcile, Position"),
    Param("cycle_max_debates", "Cycle debate cap", "Runs", "count", 0.0, 0.0, 50.0,
          "How many held positions the twice-daily cycle debates. 0 means all of them. Each debate "
          "fans out a jury, so this is the dial between thorough and expensive.",
          "Twice-daily cycle"),
    Param("debate_rounds", "Debate rounds", "Runs", "count", 2.0, 1.0, 4.0,
          "How many rounds the bull and bear argue. 1 is opening statements only — two monologues "
          "the jury never sees answered. 2 adds a rebuttal where each side must engage the other's "
          "actual case. Each extra round is two more model calls per debate.",
          "Debate — the exchange"),
    Param("debate_juror_count", "Jurors", "Runs", "count", 10.0, 1.0, 20.0,
          "How many independent jurors weigh in per debate. More jurors is a steadier verdict and a "
          "proportionally larger bill.",
          "Debate — jury"),
)

BY_KEY: dict[str, Param] = {p.key: p for p in REGISTRY}

# Settings change rarely and are read on nearly every reconciliation. A short TTL keeps a tuning
# visible within seconds without putting a query on every request.
_CACHE_TTL_SECONDS = 10.0
_cache: tuple[dict[str, float], str, float] | None = None
_lock = threading.Lock()


class SettingError(ValueError):
    """A rejected write. The message is shown to the operator verbatim, so it names the bound."""


def defaults() -> dict[str, float]:
    return {p.key: p.default for p in REGISTRY}


def get_all() -> tuple[dict[str, float], str]:
    """Current values and where they came from: 'database' or 'defaults'.

    The source is returned rather than logged because the caller must be able to SAY which it used.
    """
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache is not None and (now - _cache[2]) < _CACHE_TTL_SECONDS:
            return dict(_cache[0]), _cache[1]

    values = defaults()
    source = "database"
    try:
        with connection() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        for key, value in rows:
            if key in BY_KEY:          # an unknown key is stale data, not a new parameter
                values[key] = float(value)
    except DbUnavailable as exc:
        # Named, not swallowed: every threshold below is now the compiled default, and a breach
        # judged against a default the operator did not choose is a guardrail misreporting itself.
        logger.warning("settings unreadable, using defaults: %s", exc)
        source = "defaults"

    with _lock:
        _cache = (dict(values), source, time.monotonic())
    return values, source


def get(key: str) -> float:
    return get_all()[0][key]


def get_or(key: str, fallback: float) -> float:
    """The tuned value, or ``fallback`` when settings cannot be read.

    The shape every consumer should use. Reading a threshold must never be able to fail a request:
    a database hiccup should cost you the operator's tuning, not the page. And it must never fail
    SILENTLY into a different number than the operator set — get_all() already reports its source,
    and every surface that shows a threshold reads that.
    """
    try:
        return get_all()[0][key]
    except Exception:  # noqa: BLE001 — a settings failure is never worth failing the caller for
        logger.warning("could not read setting %s; using %s", key, fallback)
        return fallback


# Parameters that are only meaningful against each other. Each entry is (floor_key, ceiling_key).
#
# Validating fields one at a time let floor=30 / ceiling=20 be stored: both sit inside their own
# bounds, and nothing looked at the pair. Reconciliation then tested `lo <= cash <= hi` against an
# empty interval, so the cash check breached on every run whatever the actual cash was — a
# guardrail that can never pass, with nothing anywhere saying the band itself was malformed.
_BANDS: tuple[tuple[str, str], ...] = (("cash_floor_pct", "cash_ceiling_pct"),)


def _reject_inverted_band(key: str, value: float) -> None:
    """Refuse a write that would leave a floor above its ceiling.

    Checked at the WRITE, not at the read. Catching it in reconciliation would report a broken band
    every run forever; refusing it here means the operator learns at the moment they can still fix
    it, in the field they are editing.
    """
    for floor_key, ceiling_key in _BANDS:
        if key not in (floor_key, ceiling_key):
            continue
        current, _ = get_all()
        floor = value if key == floor_key else current[floor_key]
        ceiling = value if key == ceiling_key else current[ceiling_key]
        if floor > ceiling:
            raise SettingError(
                f"{BY_KEY[floor_key].label} ({floor:g}) cannot be above "
                f"{BY_KEY[ceiling_key].label} ({ceiling:g}) — that band can never be satisfied."
            )


def set_value(key: str, value: float, *, actor: str | None) -> float:
    """Write one parameter, appending to history. Returns the stored value.

    Raises SettingError for an unknown key or an out-of-bounds value — both name the problem, since
    the message is what the operator sees.
    """
    param = BY_KEY.get(key)
    if param is None:
        raise SettingError(f"{key!r} is not a tunable parameter.")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise SettingError(f"{param.label} must be a number.") from None
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        raise SettingError(f"{param.label} must be a finite number.")
    if not (param.minimum <= numeric <= param.maximum):
        raise SettingError(
            f"{param.label} must be between {param.minimum:g} and {param.maximum:g} {param.unit}."
        )
    _reject_inverted_band(key, numeric)

    with connection() as conn, conn.transaction():
        previous = conn.execute(
            "SELECT value FROM app_settings WHERE key = %s", (key,)
        ).fetchone()
        old = Decimal(str(previous[0])) if previous else None
        new = Decimal(str(numeric))
        if old is not None and old == new:
            return float(old)   # no-op: history records changes, not restatements
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_by) VALUES (%s, %s, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,"
            " updated_at = now(), updated_by = EXCLUDED.updated_by",
            (key, new, actor),
        )
        conn.execute(
            "INSERT INTO app_settings_history (key, old_value, new_value, changed_by)"
            " VALUES (%s, %s, %s, %s)",
            (key, old, new, actor),
        )

    global _cache
    with _lock:
        _cache = None   # the next read must see this, not a value up to 10s stale
    return numeric


def history(limit: int = 50) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT key, old_value, new_value, changed_at, changed_by"
            " FROM app_settings_history ORDER BY changed_at DESC, id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [
        {
            "key": r[0],
            "label": BY_KEY[r[0]].label if r[0] in BY_KEY else r[0],
            "old_value": float(r[1]) if r[1] is not None else None,
            "new_value": float(r[2]),
            "changed_at": r[3].isoformat(),
            "changed_by": r[4],
        }
        for r in rows
    ]


def reset_cache() -> None:
    """TEST SUPPORT. Nothing in the app calls this."""
    global _cache
    with _lock:
        _cache = None
