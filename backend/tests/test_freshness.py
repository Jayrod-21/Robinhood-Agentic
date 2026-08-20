"""The one staleness rule, replacing four that had drifted apart.

Four routes each parsed an ISO stamp and compared its age. Only one logged the unparseable case —
the failure mode that let a three-week-old snapshot sit unremarked in July.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.freshness import age_seconds, is_stale, parse_iso_utc

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)


def test_a_trailing_z_parses():
    """Every producer in this system emits one."""
    assert parse_iso_utc("2026-08-19T12:00:00Z") == NOW


def test_an_offset_stamp_parses_to_the_same_instant():
    assert parse_iso_utc("2026-08-19T08:00:00-04:00") == NOW


def test_a_naive_stamp_is_read_as_utc():
    """Everything here writes UTC. Guessing local time would shift an age by hours, silently."""
    assert parse_iso_utc("2026-08-19T12:00:00") == NOW


def test_an_unparseable_stamp_is_logged_not_swallowed(caplog):
    """Three of the four old copies passed silently, so an unreadable stamp was indistinguishable
    from an old one — and the remedies are completely different."""
    with caplog.at_level("WARNING", logger="agentic.freshness"):
        assert parse_iso_utc("last tuesday", field="snapshot generated_at") is None
    assert any("snapshot generated_at" in r.getMessage() for r in caplog.records), (
        "the log line must name which stamp broke"
    )


def test_absent_and_unreadable_both_count_as_stale():
    """The default is stale, and that is the point: every caller is deciding whether to present a
    number as current, and the safe answer when we cannot tell is that we cannot vouch for it."""
    assert is_stale(None, 60) is True
    assert is_stale("", 60) is True
    assert is_stale("not a date", 60) is True


def test_the_age_boundary_is_strictly_greater():
    fresh = (NOW - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
    assert is_stale(fresh, 60, now=NOW) is False, "exactly at the limit is not yet stale"
    older = (NOW - timedelta(seconds=61)).isoformat().replace("+00:00", "Z")
    assert is_stale(older, 60, now=NOW) is True


def test_a_future_stamp_reports_a_negative_age_rather_than_zero():
    """Clock skew between the broker and this host is real. Clamping it to zero would make it
    undiagnosable — and a stamp from the future is not 'very fresh', it is a symptom."""
    ahead = (NOW + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    assert age_seconds(ahead, now=NOW) == -30.0
    assert is_stale(ahead, 60, now=NOW) is False
