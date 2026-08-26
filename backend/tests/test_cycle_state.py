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
    # 12 base columns + 024's eight reconciliation columns, all NULL: this run never reconciled.
    row = (1, "close", "running", now, now, None, None, 0, None, None, None, None,
           None, None, None, None, None, None, None, None)
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
    # 12 base columns + 024's eight reconciliation columns, all NULL: this run never reconciled.
    row = (1, "close", "running", now, now, None, 15, 7, "NVDA", 25, 1, None,
           None, None, None, None, None, None, None, None)
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


# ── the reconciliation preflight, on its way OUT of the database ──────────────────────────────
#
# 024 stored it and #125 wrote it, and until now nothing read it: the cycle recorded that it had
# reasoned from a desynced slate and the page had no way to say so. Storing a finding nobody can
# see is most of the way to not having the finding.


def test_a_run_that_never_reconciled_reports_none_not_a_zeroed_object() -> None:
    """024 keeps "we never looked" distinct from "we looked and it matched" precisely because six
    weeks of unchecked cycles rendered identically to healthy ones. Flattening that on the way out
    of the database would put it straight back.

    Break: return a dict of zeros when reconciled_at is NULL.
    """
    from app.services.cycle_state import _reconciliation

    assert _reconciliation(None, None, None, None, None, None, None, None) is None


def test_a_desynced_run_reports_its_counts() -> None:
    from datetime import datetime, timezone

    from app.services.cycle_state import _reconciliation

    at = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    result = _reconciliation(at, False, 0, 5, 3, 10, 2, [{"kind": "missing", "symbol": "TSM"}])

    assert result["in_sync"] is False
    assert (result["matched"], result["drifted"], result["missing"]) == (0, 5, 3)
    assert (result["unexpected"], result["breaches"]) == (10, 2)
    assert result["checked_at"] == at.isoformat()


def test_findings_are_capped_on_the_way_out_too() -> None:
    """A book with two hundred undocumented names should not push a megabyte through a status
    endpoint the shell polls — but the TOTAL stays honest."""
    from datetime import datetime, timezone

    from app.services.cycle_state import _reconciliation

    many = [{"kind": "unexpected", "symbol": f"SYM{i}"} for i in range(200)]
    result = _reconciliation(datetime.now(timezone.utc), False, 0, 0, 0, 200, 0, many)

    assert len(result["findings"]) == 20
    assert result["findings_total"] == 200
