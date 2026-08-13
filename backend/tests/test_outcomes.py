"""services/outcomes unit tests — write discipline and journal mechanics, no real DB.

The fake connection below captures every statement so the tests can assert the property the
append-only grants depend on: the exit UPDATE touches ONLY the four granted columns and guards on
``exit_date IS NULL``. The real-grants proof (Postgres actually refusing the rest) lives in
test_outcomes_db.py; these run everywhere, fast.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from app.services import outcomes


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Returns queued results per statement, in order, and records (sql, params) calls."""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if not self.results:
            raise AssertionError(f"unexpected statement: {sql}")
        return FakeCursor(self.results.pop(0))


@pytest.fixture()
def use_fake_conn(monkeypatch):
    def _install(results):
        conn = FakeConn(results)

        from contextlib import contextmanager

        @contextmanager
        def fake_connection():
            yield conn

        monkeypatch.setattr(outcomes, "connection", fake_connection)
        return conn

    return _install


SECURITY_ROW = [(7,)]  # securities.id for the resolved symbol


# ── record_exit ──────────────────────────────────────────────────────────────────────────────


def test_record_exit_happy_path(use_fake_conn):
    conn = use_fake_conn(
        [
            SECURITY_ROW,
            [(Decimal("0.04991"), Decimal("440.79"), Decimal("0.71"))],  # UPDATE ... RETURNING
            [(31,)],  # kb INSERT ... RETURNING id
        ]
    )
    record = outcomes.record_exit(
        portfolio_id=3,
        symbol="TSM",
        entry_date=date(2026, 6, 3),
        exit_date=date(2026, 8, 13),
        exit_price=Decimal("455.00"),
        exit_reason="thesis played out",
    )
    assert record.realized_pnl == Decimal("0.71")
    assert record.shares == Decimal("0.04991")
    assert record.entry_price == Decimal("440.79")
    assert record.security_id == 7
    assert record.outcome_entry_id == 31

    _update_sql, update_params = conn.calls[1]
    # Exit prices are money: they must reach the driver as Decimal, never float (Bar §7.2 P0).
    assert isinstance(update_params["exit_price"], Decimal)

    kb_sql, _ = conn.calls[2]
    assert "INSERT INTO knowledge_base_entries" in kb_sql
    assert "'outcome'" in kb_sql


def test_exit_update_touches_only_the_four_granted_columns(use_fake_conn):
    """rh_app's UPDATE grant is exactly {exit_date, exit_price, exit_reason, realized_pnl};
    a fifth column in the SET clause would turn every exit into a permission error in prod."""
    conn = use_fake_conn(
        [SECURITY_ROW, [(Decimal("1"), Decimal("10"), Decimal("1.00"))], [(1,)]]
    )
    outcomes.record_exit(
        portfolio_id=1,
        symbol="TSM",
        entry_date=date(2026, 6, 3),
        exit_date=date(2026, 8, 13),
        exit_price=Decimal("11"),
        exit_reason="r",
    )
    update_sql = conn.calls[1][0]
    set_clause = update_sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assigned = set(re.findall(r"(\w+)\s*=", set_clause))
    assert assigned == {"exit_date", "exit_price", "exit_reason", "realized_pnl"}
    # And the once-only guard: re-exiting must be structurally impossible, not a race.
    assert "exit_date IS NULL" in update_sql


def test_double_exit_is_refused_loudly(use_fake_conn):
    use_fake_conn(
        [
            SECURITY_ROW,
            [],  # UPDATE matched no rows
            [(date(2026, 7, 1),)],  # follow-up SELECT: the lot exists, already exited
        ]
    )
    with pytest.raises(outcomes.OutcomeError, match=r"already\s+exited"):
        outcomes.record_exit(
            portfolio_id=3,
            symbol="TSM",
            entry_date=date(2026, 6, 3),
            exit_date=date(2026, 8, 13),
            exit_price=Decimal("455.00"),
            exit_reason="r",
        )


def test_exit_of_missing_lot_is_refused_with_the_other_message(use_fake_conn):
    use_fake_conn([SECURITY_ROW, [], []])
    with pytest.raises(outcomes.OutcomeError, match="nothing to exit"):
        outcomes.record_exit(
            portfolio_id=3,
            symbol="TSM",
            entry_date=date(2026, 6, 3),
            exit_date=date(2026, 8, 13),
            exit_price=Decimal("455.00"),
            exit_reason="r",
        )


def test_unknown_symbol_is_refused(use_fake_conn):
    use_fake_conn([[]])
    with pytest.raises(outcomes.OutcomeError, match="no live row in securities"):
        outcomes.record_exit(
            portfolio_id=3,
            symbol="ZZZZ",
            entry_date=date(2026, 6, 3),
            exit_date=date(2026, 8, 13),
            exit_price=Decimal("1"),
            exit_reason="r",
        )


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-455.00")])
def test_non_positive_exit_price_refused_before_any_db_touch(use_fake_conn, price):
    conn = use_fake_conn([])
    with pytest.raises(outcomes.OutcomeError, match="must be positive"):
        outcomes.record_exit(
            portfolio_id=3,
            symbol="TSM",
            entry_date=date(2026, 6, 3),
            exit_date=date(2026, 8, 13),
            exit_price=price,
            exit_reason="r",
        )
    assert conn.calls == []


def test_blank_exit_reason_refused(use_fake_conn):
    conn = use_fake_conn([])
    with pytest.raises(outcomes.OutcomeError, match="exit_reason is required"):
        outcomes.record_exit(
            portfolio_id=3,
            symbol="TSM",
            entry_date=date(2026, 6, 3),
            exit_date=date(2026, 8, 13),
            exit_price=Decimal("1"),
            exit_reason="   ",
        )
    assert conn.calls == []


# ── theses and lessons ───────────────────────────────────────────────────────────────────────


def test_record_entry_thesis(use_fake_conn):
    conn = use_fake_conn([SECURITY_ROW, [(12,)]])
    entry_id = outcomes.record_entry_thesis(symbol="TSM", title="t", thesis="compute anchor")
    assert entry_id == 12
    insert_sql = conn.calls[1][0]
    assert "'thesis'" in insert_sql


def test_empty_thesis_refused(use_fake_conn):
    conn = use_fake_conn([])
    with pytest.raises(outcomes.OutcomeError, match="empty thesis"):
        outcomes.record_entry_thesis(symbol="TSM", title="t", thesis="  ")
    assert conn.calls == []


def test_record_lesson_supports_supersedes_chain(use_fake_conn):
    conn = use_fake_conn([[(44,)]])
    entry_id = outcomes.record_lesson(title="t", lesson="corrected", supersedes_id=12)
    assert entry_id == 44
    sql, params = conn.calls[0]
    assert "supersedes_id" in sql
    assert 12 in params
    # No update/delete function exists on this module at all — corrections only supersede.
    assert not [n for n in dir(outcomes) if n.startswith(("update_", "delete_"))]


def test_empty_lesson_refused(use_fake_conn):
    conn = use_fake_conn([])
    with pytest.raises(outcomes.OutcomeError, match="not a lesson"):
        outcomes.record_lesson(title="t", lesson="\n")
    assert conn.calls == []


# ── journal append ───────────────────────────────────────────────────────────────────────────

JOURNAL = """# Agentic Robinhood — Trading Journal

## Scan Log

stuff

## Lessons Learned

_Populated as positions close._

## Next Section

tail content
"""


def test_journal_lesson_inserted_under_heading(tmp_path: Path):
    journal = tmp_path / "agentic_journal.md"
    journal.write_text(JOURNAL, encoding="utf-8")

    outcomes.append_journal_lesson(journal, symbol="TSM", lesson="exit into the gap", on=date(2026, 8, 13))

    text = journal.read_text(encoding="utf-8")
    bullet = "- **2026-08-13 — TSM:** exit into the gap"
    assert bullet in text
    # Inserted inside the Lessons section: after the heading, before the next section.
    assert text.index("## Lessons Learned") < text.index(bullet) < text.index("## Next Section")
    assert "tail content" in text  # nothing below was clobbered
    assert not list(tmp_path.glob(".journal-*")), "temp files must never survive"


def test_journal_without_heading_gets_one_appended(tmp_path: Path):
    journal = tmp_path / "j.md"
    journal.write_text("# Journal\n", encoding="utf-8")
    outcomes.append_journal_lesson(journal, symbol="V", lesson="event-gated sizing", on=date(2026, 8, 13))
    text = journal.read_text(encoding="utf-8")
    assert "## Lessons Learned" in text
    assert "- **2026-08-13 — V:** event-gated sizing" in text


def test_journal_missing_file_is_a_loud_error(tmp_path: Path):
    with pytest.raises(outcomes.OutcomeError, match="journal not found"):
        outcomes.append_journal_lesson(tmp_path / "nope.md", symbol="V", lesson="x")


def test_journal_empty_lesson_refused(tmp_path: Path):
    journal = tmp_path / "j.md"
    journal.write_text("# Journal\n", encoding="utf-8")
    with pytest.raises(outcomes.OutcomeError, match="not a lesson"):
        outcomes.append_journal_lesson(journal, symbol="V", lesson=" ")
