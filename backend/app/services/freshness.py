"""Is this timestamp recent enough to act on? One implementation, four former copies.

WHY THIS IS ITS OWN MODULE
    Four routes each parsed an ISO-8601 stamp tolerating a trailing Z, compared it against a
    maximum age, and decided "stale". They had drifted: data_trust logged the unparseable case,
    reconciliation and position swallowed it silently, market_context caught a narrower exception
    than the other three. Only one of the four recorded the failure mode that actually bit this
    project — a snapshot that sat unnoticed for three weeks in July.

    Freshness is load-bearing here. Every page that shows a number also shows how old it is, and
    the whole honesty thesis rests on that second half being right. Four implementations of a
    load-bearing rule is three chances for one of them to be wrong, and the fifth endpoint would
    have made a fifth copy.

THE RULE, IN ONE PLACE
    Absent or unparseable is STALE, and says so out loud. Freshness is proven, never assumed: a
    stamp nobody can read is not evidence of anything, and treating it as fresh is how a
    three-week-old snapshot goes unremarked on a page whose job is to say what to trust.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("agentic.freshness")


def parse_iso_utc(value: object, *, field: str = "timestamp") -> datetime | None:
    """An ISO-8601 stamp as an aware UTC datetime, or None if it cannot be read.

    Tolerates the trailing ``Z`` that every producer here emits. A naive stamp is assumed UTC —
    every writer in this system works in UTC, and guessing local time would shift an age by hours.

    ``field`` names the thing in the log line, because "not ISO-8601" without a subject sends the
    next reader hunting through four routes for which stamp broke.
    """
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        # Logged, never swallowed. Three of the four copies passed silently, so an unreadable stamp
        # was indistinguishable from an old one — and the remedies are completely different.
        logger.warning("%s is not ISO-8601 and cannot be aged: %r", field, value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_seconds(value: object, *, field: str = "timestamp", now: datetime | None = None) -> float | None:
    """How old the stamp is, or None when it cannot be read.

    A stamp in the FUTURE returns a negative age rather than being clamped: clock skew between the
    broker and this host is real, and hiding it behind a zero would make it undiagnosable.
    """
    parsed = parse_iso_utc(value, field=field)
    if parsed is None:
        return None
    return ((now or datetime.now(timezone.utc)) - parsed).total_seconds()


def is_stale(value: object, max_age_seconds: float, *, field: str = "timestamp",
             now: datetime | None = None) -> bool:
    """True when the stamp is older than ``max_age_seconds``, absent, or unreadable.

    The default is STALE, and that is the whole point. Every caller is deciding whether to present a
    number as current; the safe answer when we cannot tell is that we cannot vouch for it.
    """
    age = age_seconds(value, field=field, now=now)
    if age is None:
        return True
    return age > max_age_seconds
