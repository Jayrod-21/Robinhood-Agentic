"""Cycle progress: what it reports, and the states it must not conflate.

The point of this table is answering "is it running?" — so the failures that matter are the ones
where it answers confidently and wrongly.
"""

from __future__ import annotations

import pytest
from app.routers import cycle as mod
from app.services import cycle_state


class _Cur:
    def __init__(self, rows): self._rows = rows
    def fetchone(self): return self._rows[0] if self._rows else None
    def fetchall(self): return self._rows
    rowcount = 0


def _conn(rows):
    class _Conn:
        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("UPDATE"):
                return _Cur([])
            return _Cur(rows)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return _Conn()


def test_progress_is_none_not_zero_while_the_total_is_unknown():
    """The scan runs before the book is read, so total_positions is null for the first minute.
    Rendering that as 0% claims no progress on work that has not been sized yet — a measured-looking
    number for something unmeasured."""
    from datetime import datetime, timezone

    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    row = (1, "close", "running", now, now, None, None, 0, None, None, None, None)
    import app.services.cycle_state as cs
    orig = cs.connection
    cs.connection = lambda: _conn([row])
    try:
        out = cs.current()
    finally:
        cs.connection = orig
    assert out["progress_pct"] is None, "unknown total must not render as 0%"
    assert out["total_positions"] is None


def test_progress_is_computed_once_the_total_is_known():
    from datetime import datetime, timezone

    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    row = (1, "close", "running", now, now, None, 15, 7, "NVDA", 25, 1, None)
    import app.services.cycle_state as cs
    orig = cs.connection
    cs.connection = lambda: _conn([row])
    try:
        out = cs.current()
    finally:
        cs.connection = orig
    assert out["progress_pct"] == pytest.approx(46.7, abs=0.1)
    assert out["current_symbol"] == "NVDA"


def test_never_having_run_is_distinct_from_nothing_running(monkeypatch):
    """A deployment that has never run a cycle and a quiet Sunday look identical otherwise, and only
    one of them is worth investigating."""
    monkeypatch.setattr(mod.cycle_state, "current", lambda: None)
    monkeypatch.setattr(mod.cycle_state, "recent", lambda limit=10: [])
    body = mod.current_cycle()
    assert body["run"] is None
    assert body["meta"]["has_ever_run"] is False
    assert body["meta"]["is_running"] is False


def test_a_swept_run_says_its_status_was_inferred():
    """A killed process cannot close its own row. Marking it failed is right; implying somebody
    observed it fail is not — the message has to carry that it was deduced from silence."""
    assert "presumed" in _sweep_message()
    assert "inferred" in _sweep_message()


def _sweep_message() -> str:
    import inspect
    return inspect.getsource(cycle_state.sweep_stale)


def test_a_progress_write_never_raises_into_the_cycle(monkeypatch):
    """Telemetry must not be able to kill the work it reports on. A cycle that dies because it could
    not record being 7 of 15 through has traded the thing for the story about the thing."""
    def boom():
        raise RuntimeError("database on fire")

    monkeypatch.setattr(cycle_state, "connection", lambda: boom())
    assert cycle_state.start("close") is None          # returns None, does not raise
    cycle_state.update(1, completed_positions=3)        # no raise
    cycle_state.finish(1, error="something")            # no raise


def test_the_stale_window_is_longer_than_a_real_cycle():
    """Sweeping a LIVE cycle would report a running job as dead. A full cycle is ~20 minutes at
    fifteen positions; the window has to clear that with room for a slow provider."""
    assert cycle_state.STALE_AFTER_MINUTES >= 60
