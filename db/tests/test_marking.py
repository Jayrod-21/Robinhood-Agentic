"""Integration tests for the marking job (db/mark_portfolios.py, issue #36) against a real
throwaway Postgres carrying the ACTUAL repo migrations.

The three revert-proof properties this file exists to hold, each proven red by mutation during
the fix-pass for issue #36 (revert evidence in the issue/PR discussion):

  1. THE FORMULA. Marking is Σ (as-traded shares × split factor) × RAW close + cash. A revert to
     Σ shares × adj_close fails `test_marking_values_across_a_split_with_raw_close` numerically
     (the post-split sessions come out wrong by the split factor) and
     `test_module_never_references_adj_close` textually.
  2. IDEMPOTENCY. Re-running a window must not double-write or drift.
     `test_rerun_is_idempotent` fails if ON CONFLICT DO NOTHING is removed (unique violation) or
     if a re-run mutates any stored column.
  3. HONEST LABELS. Backfill marks say mark_kind='backfill'.
     `test_backfill_marks_are_labelled_backfill` fails if the job labels historical marks 'live'
     (ck_prd_mark_window then rejects the insert — migration 009's designed failure mode).

Never touches the live rh-db — the container here is ephemeral and dies with the session.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
    from testcontainers.community.postgres import PostgresContainer
except ImportError:  # pragma: no cover
    from testcontainers.postgres import PostgresContainer

import mark_portfolios as mp
from migrate import main as migrate_main

REPO_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

# Same major as the live stack (docker-compose.db.yml pins postgres:16-alpine by digest).
PG_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def marking_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


def _admin_url(pg: PostgresContainer) -> str:
    return (
        f"postgresql://{pg.username}:{pg.password}"
        f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
    )


@pytest.fixture
def db(marking_pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh, fully-migrated database per test, exported as DATABASE_URL (the job's contract)."""
    name = f"mdb_{uuid.uuid4().hex[:12]}"
    admin = _admin_url(marking_pg)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    monkeypatch.setenv("DATABASE_URL", url)
    assert migrate_main(["up", "--migrations-dir", str(REPO_MIGRATIONS)]) == 0
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def q(url: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


# ── seed helpers ──────────────────────────────────────────────────────────────────────────────


def _seed_calendar(db: str, days: list[date]) -> None:
    for d in days:
        open_ts = datetime.combine(d, time(14, 30), tzinfo=timezone.utc)
        close_ts = datetime.combine(d, time(21, 0), tzinfo=timezone.utc)
        q(db, "INSERT INTO market_calendar (trade_date, is_trading_day, session_open, session_close) "
              "VALUES (%s, TRUE, %s, %s)", (d, open_ts, close_ts))


def _seed_security(db: str, symbol: str) -> int:
    return q(db, "INSERT INTO securities (symbol) VALUES (%s) RETURNING id", (symbol,))[0][0]


def _seed_bars(db: str, sec_id: int, closes: dict[date, str],
               adj: dict[date, tuple[str, str]] | None = None) -> None:
    """Insert bars at the given raw closes; `adj` optionally supplies (adj_close, factor) so a
    formula revert to adj_close produces a WRONG number rather than a NULL-driven skip."""
    for d, close in closes.items():
        c = Decimal(close)
        adj_close, factor = (None, None)
        if adj and d in adj:
            adj_close, factor = Decimal(adj[d][0]), Decimal(adj[d][1])
        q(db, "INSERT INTO price_bars_daily (security_id, trade_date, open, high, low, close, "
              "adj_close, split_adj_factor, volume) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1000)",
          (sec_id, d, c, c, c, c, adj_close, factor))


def _seed_split(db: str, sec_id: int, ex_date: date, ratio: str) -> None:
    q(db, "INSERT INTO corporate_actions (security_id, action_type, ex_date, split_ratio) "
          "VALUES (%s, 'split', %s, %s)", (sec_id, ex_date, Decimal(ratio)))


def _seed_portfolio(db: str, *, inception: date, base: str, cash: str) -> int:
    agent = q(db, "INSERT INTO agents (agent_key, version, kind) "
                  "VALUES ('bull', 1, 'persona') RETURNING id")[0][0]
    return q(db, "INSERT INTO paper_portfolios (kind, agent_id, strategy_mode, inception_date, "
                 "base_value, cash) VALUES ('agent_composite', %s, 'rebalanced', %s, %s, %s) "
                 "RETURNING id", (agent, inception, Decimal(base), Decimal(cash)))[0][0]


def _seed_lot(db: str, pid: int, sec_id: int, entry: date, shares: str, price: str,
              exit_date: date | None = None, exit_price: str | None = None) -> None:
    q(db, "INSERT INTO paper_portfolio_positions (portfolio_id, security_id, entry_date, shares, "
          "entry_price, exit_date, exit_price) VALUES (%s, %s, %s, %s, %s, %s, %s)",
      (pid, sec_id, entry, Decimal(shares), Decimal(price), exit_date,
       Decimal(exit_price) if exit_price else None))


def marks(db: str, pid: int) -> list[tuple]:
    return q(db, "SELECT trade_date, market_value, daily_return, cumulative_return, mark_kind, "
                 "source_id, priced_as_of, created_at FROM portfolio_returns_daily "
                 "WHERE portfolio_id = %s ORDER BY trade_date", (pid,))


# The split scenario used throughout: a lot entered BEFORE a 10-for-1 split and held across it —
# the exact shape that made Σ shares × adj_close 40x wrong for a pre-2021 NVDA lot (B-S3).
SPLIT_DAYS = [date(2024, 6, 5), date(2024, 6, 6), date(2024, 6, 7),
              date(2024, 6, 10), date(2024, 6, 11)]


def _seed_split_scenario(db: str) -> int:
    """4 shares @ $1000 bought 2024-06-05; 10:1 split ex 2024-06-10; $6,000 cash alongside.
    adj_close is populated the way a real post-adjust archive would carry it (factor 10 before
    the split, 1 after), so a formula revert produces a wrong VALUE, not a skipped session."""
    _seed_calendar(db, SPLIT_DAYS)
    sec = _seed_security(db, "NVDX")
    _seed_split(db, sec, date(2024, 6, 10), "10")
    _seed_bars(
        db, sec,
        {SPLIT_DAYS[0]: "1000", SPLIT_DAYS[1]: "1010", SPLIT_DAYS[2]: "1200",
         SPLIT_DAYS[3]: "121", SPLIT_DAYS[4]: "122"},
        adj={SPLIT_DAYS[0]: ("100", "10"), SPLIT_DAYS[1]: ("101", "10"),
             SPLIT_DAYS[2]: ("120", "10"), SPLIT_DAYS[3]: ("121", "1"),
             SPLIT_DAYS[4]: ("122", "1")},
    )
    pid = _seed_portfolio(db, inception=SPLIT_DAYS[0], base="10000", cash="6000")
    _seed_lot(db, pid, sec, SPLIT_DAYS[0], "4", "1000")
    return pid


# ── 1. the formula ────────────────────────────────────────────────────────────────────────────


def test_marking_values_across_a_split_with_raw_close(db: str) -> None:
    """Raw × as-traded marking across a 10:1 split, hand-computed. The two post-split sessions
    are the revert detectors: Σ shares × adj_close gives 4×121+6000 = 6,484 there — wrong by the
    split factor — while the correct mark carries 40 effective shares × the raw $121 close."""
    pid = _seed_split_scenario(db)
    assert mp.main(["backfill", "--from", "2024-06-05", "--to", "2024-06-11"]) == 0

    rows = marks(db, pid)
    assert [(r[0], r[1]) for r in rows] == [
        (SPLIT_DAYS[0], Decimal("10000.00")),   # 4×1000 + 6000
        (SPLIT_DAYS[1], Decimal("10040.00")),   # 4×1010 + 6000
        (SPLIT_DAYS[2], Decimal("10800.00")),   # 4×1200 + 6000
        (SPLIT_DAYS[3], Decimal("10840.00")),   # 40×121 + 6000  ← the split day
        (SPLIT_DAYS[4], Decimal("10880.00")),   # 40×122 + 6000
    ]

    # Return chaining, from the STORED values: first mark NULL; the rest hand-recomputable.
    returns = {r[0]: r[2] for r in rows}
    assert returns[SPLIT_DAYS[0]] is None
    assert returns[SPLIT_DAYS[1]] == Decimal("0.00400000")            # 10040/10000 - 1
    assert returns[SPLIT_DAYS[3]] == Decimal("0.00370370")            # 10840/10800 - 1
    assert {r[0]: r[3] for r in rows}[SPLIT_DAYS[4]] == Decimal("0.08800000")  # 10880/10000 - 1

    # "Recomputable by hand from the stored lots and prices" (issue #36's done-criterion),
    # asserted as an independent SQL recomputation of the split day's mark.
    by_hand = q(db, """
        SELECT sum(p.shares * split_factor_between(p.security_id, p.entry_date, %(d)s) * b.close)
               + (SELECT cash FROM paper_portfolios WHERE id = %(pid)s)
        FROM paper_portfolio_positions p
        JOIN price_bars_daily b ON b.security_id = p.security_id AND b.trade_date = %(d)s
        WHERE p.portfolio_id = %(pid)s
    """, {"d": SPLIT_DAYS[3], "pid": pid})[0][0]
    assert by_hand == Decimal("10840.00")


def test_module_sql_never_references_adj_close() -> None:
    """The textual pin behind the numeric test above: adj_close is 'NEVER a marking price'
    (catalog comment, B-S3). Docstrings may (and do) explain the ban; nothing EXECUTABLE may
    mention the column, so every string literal outside a docstring — which is where all of the
    module's SQL lives — is scanned."""
    import ast

    tree = ast.parse(Path(mp.__file__).read_text(encoding="utf-8"))
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)
    }
    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node not in docstrings and "adj_close" in node.value
    ]
    assert offenders == []


# ── 2. idempotency ────────────────────────────────────────────────────────────────────────────


def test_rerun_is_idempotent(db: str) -> None:
    """A second run over the same window must add nothing and change nothing — including
    created_at and priced_as_of, which would betray a delete-and-rewrite. Removing the insert's
    ON CONFLICT DO NOTHING turns this test red with a unique-violation exit."""
    pid = _seed_split_scenario(db)
    assert mp.main(["backfill", "--from", "2024-06-05", "--to", "2024-06-11"]) == 0
    first = marks(db, pid)
    assert mp.main(["backfill", "--from", "2024-06-05", "--to", "2024-06-11"]) == 0
    assert marks(db, pid) == first


def test_rerun_never_rewrites_and_reports_drift(db: str) -> None:
    """If price data changes under scored history, the re-run must LEAVE THE MARK ALONE and fail
    loudly — append-only means corrections belong to the migration role, not to a quiet re-mark."""
    pid = _seed_split_scenario(db)
    assert mp.main(["backfill", "--from", "2024-06-05", "--to", "2024-06-11"]) == 0
    before = marks(db, pid)
    q(db, "UPDATE price_bars_daily SET close = 999, high = 999, low = 999, open = 999 "
          "WHERE trade_date = %s", (SPLIT_DAYS[1],))
    assert mp.main(["backfill", "--from", "2024-06-05", "--to", "2024-06-11"]) == mp.EXIT_VALIDATION
    assert marks(db, pid) == before  # drift reported, nothing repaired in place


# ── 3. honest labels ──────────────────────────────────────────────────────────────────────────


def test_backfill_marks_are_labelled_backfill(db: str) -> None:
    """Historical marks must say mark_kind='backfill' (migration 009). If the job labelled them
    'live', ck_prd_mark_window would reject every row priced > 4 days after its trading day —
    which is exactly how a mislabelling revert turns this test red."""
    pid = _seed_split_scenario(db)
    assert mp.main(["backfill", "--from", "2024-06-05", "--to", "2024-06-11"]) == 0
    kinds = {r[4] for r in marks(db, pid)}
    assert kinds == {"backfill"}
    # And the schema's honesty gate is live: the same historical row labelled 'live' is unstorable.
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_prd_mark_window"):
        q(db, "INSERT INTO portfolio_returns_daily (portfolio_id, trade_date, market_value, "
              "priced_as_of, mark_kind) VALUES (%s, %s, 1, now(), 'live')",
          (pid, date(2024, 6, 12)))


def test_live_mode_refuses_stale_dates_and_labels_fresh_marks_live(db: str) -> None:
    today = datetime.now(timezone.utc).date()
    days = [today - timedelta(days=2), today - timedelta(days=1), today]
    _seed_calendar(db, days)
    sec = _seed_security(db, "LIVX")
    _seed_bars(db, sec, {d: "50" for d in days})
    pid = _seed_portfolio(db, inception=days[0], base="1000", cash="500")
    _seed_lot(db, pid, sec, days[0], "10", "50")

    # A live mark for a historical date is refused before any SQL — the remedy is named backfill.
    assert mp.main(["live", "--date", "2024-06-10"]) == mp.EXIT_VALIDATION
    assert marks(db, pid) == []

    # Chain: backfill the first two sessions, then a live mark for today on top of them.
    assert mp.main(["backfill", "--from", days[0].isoformat(), "--to", days[1].isoformat()]) == 0
    assert mp.main(["live"]) == 0
    rows = marks(db, pid)
    assert [(r[0], r[4]) for r in rows] == [(days[0], "backfill"), (days[1], "backfill"),
                                            (days[2], "live")]
    assert rows[2][1] == Decimal("1000.00")          # 10×50 + 500
    assert rows[2][2] == Decimal("0")                # flat close-over-close, chained across kinds


# ── coverage holes are loud, and marking resumes cleanly ──────────────────────────────────────


def test_missing_bar_skips_the_session_loudly_then_backfills_idempotently(db: str) -> None:
    days = [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)]
    _seed_calendar(db, days)
    sec = _seed_security(db, "HOLE")
    _seed_bars(db, sec, {days[0]: "10", days[2]: "12"})  # no bar on days[1]
    pid = _seed_portfolio(db, inception=days[0], base="100", cash="0")
    _seed_lot(db, pid, sec, days[0], "10", "10")

    # The hole makes the run exit non-zero; the sessions AROUND it are still marked, and the far
    # side must not disguise a two-session move as a daily return.
    assert mp.main(["backfill", "--from", "2024-03-04", "--to", "2024-03-06"]) == mp.EXIT_VALIDATION
    rows = marks(db, pid)
    assert [r[0] for r in rows] == [days[0], days[2]]
    assert rows[1][2] is None  # daily_return across the hole is undefined, not a fake daily move

    # Repair the gap and re-run: the hole fills, nothing else moves, and the filled session
    # chains off the mark that precedes it.
    _seed_bars(db, sec, {days[1]: "11"})
    assert mp.main(["backfill", "--from", "2024-03-04", "--to", "2024-03-06"]) == 0
    rows = marks(db, pid)
    assert [(r[0], r[1]) for r in rows] == [
        (days[0], Decimal("100.00")), (days[1], Decimal("110.00")), (days[2], Decimal("120.00")),
    ]
    assert rows[1][2] == Decimal("0.10000000")
    # The far-side mark keeps its honest NULL: recomputing it would rewrite scored history.
    assert rows[2][2] is None


# ── cash reconstruction from the lot ledger ───────────────────────────────────────────────────


def test_cash_is_reconstructed_from_the_ledger_for_historical_marks(db: str) -> None:
    """paper_portfolios.cash is TODAY's balance; marks before a later entry/exit must back those
    flows out. Base 1000 → buy A (100) on d1 → buy B (100) on d3 → sell A for 120 on d4 leaves
    cash_now = 920, but the mark on d1 must value cash at 900, and on d3 at 800."""
    d1, d2, d3, d4, d5 = days = [date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3),
                                 date(2024, 7, 5), date(2024, 7, 8)]
    _seed_calendar(db, days)
    a, b = _seed_security(db, "AAA"), _seed_security(db, "BBB")
    _seed_bars(db, a, {d1: "10", d2: "11", d3: "11"})
    _seed_bars(db, b, {d3: "20", d4: "20", d5: "22"})
    pid = _seed_portfolio(db, inception=d1, base="1000", cash="920")
    _seed_lot(db, pid, a, d1, "10", "10", exit_date=d4, exit_price="12")
    _seed_lot(db, pid, b, d3, "5", "20")

    assert mp.main(["backfill", "--from", d1.isoformat(), "--to", d5.isoformat()]) == 0
    assert [(r[0], r[1]) for r in marks(db, pid)] == [
        (d1, Decimal("1000.00")),  # 10×10 + (920 + 100 entry-after − 120 exit-after) = 100 + 900
        (d2, Decimal("1010.00")),  # 10×11 + 900
        (d3, Decimal("1010.00")),  # 10×11 + 5×20 + (920 − 120) = 210 + 800
        (d4, Decimal("1020.00")),  # A exited on d4: 5×20 + 920 — proceeds live in cash, once
        (d5, Decimal("1030.00")),  # 5×22 + 920
    ]


# ── provenance ────────────────────────────────────────────────────────────────────────────────


def test_provenance_row_records_mode_and_price_only_basis(db: str) -> None:
    pid = _seed_split_scenario(db)
    assert mp.main(["backfill", "--from", "2024-06-05", "--to", "2024-06-11"]) == 0

    sources = q(db, "SELECT id, provider, dataset, period_start, period_end, row_count, notes "
                    "FROM data_sources WHERE provider = 'marking_job'")
    assert len(sources) == 1
    sid, _provider, dataset, p_start, p_end, row_count, notes = sources[0]
    assert (dataset, p_start, p_end) == ("portfolio_marks", date(2024, 6, 5), date(2024, 6, 11))
    assert row_count == 5
    assert "mode=backfill" in notes
    assert "price_only" in notes  # the basis statement evaluation_runs.return_basis must echo
    assert {r[5] for r in marks(db, pid)} == {sid}  # every mark carries the run's source_id


def test_no_portfolios_and_dry_run_write_nothing(db: str) -> None:
    _seed_calendar(db, [date(2024, 1, 2)])
    assert mp.main(["backfill", "--from", "2024-01-02", "--to", "2024-01-02"]) == 0
    assert q(db, "SELECT count(*) FROM data_sources WHERE provider = 'marking_job'")[0][0] == 0

    pid = _seed_split_scenario(db)
    assert mp.main(["backfill", "--from", "2024-06-05", "--to", "2024-06-11", "--dry-run"]) == 0
    assert marks(db, pid) == []
    assert q(db, "SELECT count(*) FROM data_sources WHERE provider = 'marking_job'")[0][0] == 0
