"""Regression tests for the Phase A fix-pass blockers — loaders against a real throwaway Postgres.

One test (or pair) per blocker, each proven to go red when its defence is reverted on a scratch
copy (see docs/fixpass/FIX_REPORT_phaseA.md for the revert evidence):

  B-1  — a provider failure fails the fetch run loudly (and a total failure aborts as a
         connection error) instead of silently becoming "no actions" and exit 0.
  B-2  — the minute loader survives corrupt archive members: report, skip, continue, exit 1.
  B-S1 — the adjustment excludes splits with ex_date after the declared as-of; the unbounded
         function no longer exists.
  B-S2 — evaluation_runs.return_basis is required: a price-only number cannot be stored as an
         unlabelled "total return".
  B-S3/B-S4 — the catalog says true things: marking is raw-close domain, the stored close is
         the 15:59 bar; and the 16:00 auction bucket stays excluded BY DESIGN (behaviour pin —
         the semantics review explicitly warns against "fixing" B-S4 by moving the bound).
  B-S5 — splice splits a recycled ticker into two identities (actions follow the CURRENT
         holder), and infer marks dead names delisted.
  B-N1 — the provider comparison is adjusted-to-adjusted: raw closes are never compared
         against the provider's split-adjusted series (which red-lit a CORRECT database), and
         per-symbol failures are collected, not raised on the first.
  B-N2 — internal holes are classified by EVIDENCE (price discontinuity net of recorded
         splits, stored in price_gap_audit), spliced from the audit rather than a guessed
         length threshold, and check 7 fails while any out-of-band hole is unresolved.
  S-5  — an ON CONFLICT no-op with a DIFFERENT provider value warns and counts, never silent.

Never touches the live rh-db — the container is ephemeral and dies with the session.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import sys
import types
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

try:  # testcontainers >= 4.x moved community modules; keep the fallback for older installs
    from testcontainers.community.postgres import PostgresContainer
except ImportError:  # pragma: no cover
    from testcontainers.postgres import PostgresContainer

import load_corporate_actions as lca
import load_daily_bars as ldb
import load_delistings as ldel
import load_minute_bars as lmb
import load_reference_data as lrd
import verify_daily_series as vds
from migrate import main as migrate_main

REPO_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PG_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def loaders_pg() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg


def _admin_url(pg: PostgresContainer) -> str:
    return (
        f"postgresql://{pg.username}:{pg.password}"
        f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
    )


@pytest.fixture
def db(loaders_pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh, fully-migrated database per test, exported as DATABASE_URL (the loaders' contract)."""
    name = f"ldb_{uuid.uuid4().hex[:12]}"
    admin = _admin_url(loaders_pg)
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


def _seed_security(db_url: str, symbol: str) -> int:
    return q(db_url, "INSERT INTO securities (symbol) VALUES (%s) RETURNING id", (symbol,))[0][0]


def _seed_bars(db_url: str, sec_id: int, closes: dict[date, str]) -> None:
    for d, close in closes.items():
        c = Decimal(close)
        q(
            db_url,
            "INSERT INTO price_bars_daily (security_id, trade_date, open, high, low, close, volume) "
            "VALUES (%s, %s, %s, %s, %s, %s, 1000)",
            (sec_id, d, c, c, c, c),
        )


# ── B-1: provider failures are loud, counted, and non-zero ────────────────────────────────────
def test_b1_provider_failure_fails_the_run(
    db: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_security(db, "AAPL")

    def broken(symbol: str):
        raise lca.ProviderError(f"{symbol}: provider error: no egress")

    monkeypatch.setattr(lca, "fetch_actions", broken)
    with caplog.at_level(logging.WARNING):
        rc = lca.main(["fetch", "--symbols", "AAPL"])
    # "Could not ask" must be distinguishable from "no actions" all the way to the exit code:
    # the old code logged at DEBUG, counted nothing, and returned 0.
    assert rc == lca.EXIT_VALIDATION
    assert any("could NOT be asked" in r.message for r in caplog.records)


def test_b1_total_failure_short_circuits_as_connection_error(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    symbols = [f"ZZT{i}" for i in range(lca.CONSECUTIVE_PROVIDER_FAILURES_ABORT + 3)]
    for s in symbols:
        _seed_security(db, s)

    calls = {"n": 0}

    def broken(symbol: str):
        calls["n"] += 1
        raise lca.ProviderError(f"{symbol}: provider error: no egress")

    monkeypatch.setattr(lca, "fetch_actions", broken)
    rc = lca.main(["fetch", "--symbols", *symbols])
    assert rc == lca.EXIT_CONNECTION
    # Aborted in seconds, not hours: only the short-circuit budget was burned.
    assert calls["n"] == lca.CONSECUTIVE_PROVIDER_FAILURES_ABORT


# ── S-5: conflicting provider value warns instead of vanishing ────────────────────────────────
def test_s5_conflicting_action_value_warns(
    db: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sec = _seed_security(db, "NVDA")
    q(
        db,
        "INSERT INTO corporate_actions (security_id, action_type, ex_date, split_ratio) "
        "VALUES (%s, 'split', '2021-07-20', 4.0)",
        (sec,),
    )

    monkeypatch.setattr(lca, "fetch_actions", lambda _s: ([(date(2021, 7, 20), 5.0)], []))
    with caplog.at_level(logging.WARNING):
        rc = lca.main(["fetch", "--symbols", "NVDA"])
    assert rc == lca.EXIT_OK
    assert any("already recorded" in r.message for r in caplog.records)
    # The stored value is deliberately NOT overwritten — the disagreement is surfaced, not resolved.
    assert q(db, "SELECT split_ratio FROM corporate_actions WHERE security_id = %s", (sec,)) == [
        (Decimal("4.00000000"),)
    ]


# ── B-2: the minute loader survives the corrupt archive it exists to load ─────────────────────
def _write_minute_file(path: Path, trade_date: date, rows: list[list[str]]) -> None:
    with gzip.open(path, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(lmb.EXPECTED_HEADER)
        w.writerows(rows)


def test_b2_corrupt_members_are_skipped_reported_and_nonzero(db: str, tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    ns = int(datetime(2024, 7, 5, 14, 0, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
    _write_minute_file(
        root / "2024-07-05.csv.gz", date(2024, 7, 5),
        [["AAPL", "100", "10.0", "10.5", "11.0", "9.0", str(ns), "5"]],
    )
    # 14 of the archive's 15 corrupt members look like this: not a gzip stream at all.
    (root / "2024-07-08.csv.gz").write_bytes(b"this is not a gzip stream")
    # …and one (2024-12-10) inflates for a while and then dies mid-stream: emulate with a
    # truncated member, which fails the same lazily-surfacing way.
    buf = io.BytesIO()
    with gzip.open(buf, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(lmb.EXPECTED_HEADER)
        for i in range(20_000):
            w.writerows([["AAPL", "100", "10.0", "10.5", "11.0", "9.0", str(ns + i), "5"]])
    intact = buf.getvalue()
    (root / "2024-07-09.csv.gz").write_bytes(intact[: len(intact) // 2])

    rc = lmb.main(["--root", str(root)])
    # The run COMPLETES: exit 1 names the corrupt files instead of a traceback on the first one.
    # (The old loader died with an uncaught zlib.error and never reached the rest of the archive.)
    assert rc == lmb.EXIT_VALIDATION
    # The healthy file's rows landed despite two corrupt siblings.
    assert q(db, "SELECT count(*) FROM price_bars_minute")[0][0] == 1
    # Nothing partial was recorded for the corrupt files (per-file transaction held).
    assert q(db, "SELECT count(*) FROM data_sources WHERE dataset = 'minute_bars'")[0][0] == 1


# ── B-S1: the adjustment is bounded by the declared as-of ─────────────────────────────────────
def test_bs1_post_asof_splits_are_excluded(db: str) -> None:
    sec = _seed_security(db, "PAVS")
    _seed_bars(db, sec, {
        date(2025, 9, 29): "2.08",
        date(2025, 9, 30): "1.04",
        date(2025, 10, 1): "1.05",
    })
    # An in-window split between the first and second bar…
    q(
        db,
        "INSERT INTO corporate_actions (security_id, action_type, ex_date, split_ratio) "
        "VALUES (%s, 'split', '2025-09-30', 2.0)",
        (sec,),
    )
    # …and a post-archive reverse split the provider knows about but nobody on 2025-10-01 could.
    q(
        db,
        "INSERT INTO corporate_actions (security_id, action_type, ex_date, split_ratio) "
        "VALUES (%s, 'split', '2025-12-18', 0.01)",
        (sec,),
    )

    assert lca.main(["adjust"]) == lca.EXIT_OK

    rows = q(
        db,
        "SELECT trade_date, split_adj_factor, adj_close FROM price_bars_daily "
        "WHERE security_id = %s ORDER BY trade_date",
        (sec,),
    )
    # The bar before the in-window split carries factor 2; the later bars carry factor 1. The
    # 2025-12-18 reverse split is EXCLUDED everywhere — under the old unbounded product the
    # factors would be 0.02 / 0.01 / 0.01 and the $1.04 close would "adjust" to $104.
    assert rows == [
        (date(2025, 9, 29), Decimal("2.000000000000"), Decimal("1.0400000000")),
        (date(2025, 9, 30), Decimal("1.000000000000"), Decimal("1.0400000000")),
        (date(2025, 10, 1), Decimal("1.000000000000"), Decimal("1.0500000000")),
    ]
    # The bound is declared and auditable…
    assert q(db, "SELECT adjustment_as_of FROM price_adjustment_state")[0][0] == date(2025, 10, 1)
    # …the unbounded function no longer exists to misuse…
    assert q(db, "SELECT count(*) FROM pg_proc WHERE proname = 'split_factor_after'")[0][0] == 0
    # …and verify's factor cross-check agrees.
    assert lca.main(["verify"]) == lca.EXIT_OK


# ── B-S2: a price-only number cannot be stored as an unlabelled total return ──────────────────
def test_bs2_return_basis_is_required_and_closed(db: str) -> None:
    aid = q(db, "INSERT INTO agents (agent_key, version, kind) VALUES ('blind', 1, 'blind') RETURNING id")[0][0]
    pid = q(
        db,
        "INSERT INTO paper_portfolios (kind, agent_id, inception_date) "
        "VALUES ('blind', %s, CURRENT_DATE - 10) RETURNING id",
        (aid,),
    )[0][0]
    q(
        db,
        "INSERT INTO market_calendar (trade_date, is_trading_day, session_open, session_close) "
        "SELECT d::date, true, (d::date + time '09:30') AT TIME ZONE 'America/New_York', "
        "(d::date + time '16:00') AT TIME ZONE 'America/New_York' "
        "FROM generate_series(CURRENT_DATE - 1, CURRENT_DATE, interval '1 day') AS d",
    )

    base_cols = (
        "portfolio_id, window_start, window_end, n_observations, min_n_for_ranking, "
        "risk_free_annual, inputs_as_of, rf_conversion, expected_sessions"
    )
    base_vals = "%s, CURRENT_DATE - 1, CURRENT_DATE, 0, 21, 0.05, now(), 'simple', 2"

    # Omitting return_basis is unstorable — the column that stops a price-only Sharpe wearing a
    # total-return label.
    with pytest.raises(psycopg.errors.NotNullViolation, match="return_basis"):
        q(db, f"INSERT INTO evaluation_runs ({base_cols}) VALUES ({base_vals})", (pid,))
    # The vocabulary is closed: no third, vaguer label.
    with pytest.raises(psycopg.errors.CheckViolation):
        q(
            db,
            f"INSERT INTO evaluation_runs ({base_cols}, return_basis) VALUES ({base_vals}, 'total')",
            (pid,),
        )
    q(
        db,
        f"INSERT INTO evaluation_runs ({base_cols}, return_basis) VALUES ({base_vals}, 'price_only')",
        (pid,),
    )
    assert q(db, "SELECT return_basis, coverage_ratio FROM evaluation_runs")[0] == (
        "price_only", Decimal("0.000000"),
    )


# ── B-S3 / B-S4: the catalog tells the truth, and the session bound stays put ─────────────────
def test_bs3_bs4_catalog_comments_are_true(db: str) -> None:
    def col_comment(table: str, col: str) -> str:
        return q(
            db,
            "SELECT col_description(%s::regclass, attnum) FROM pg_attribute "
            "WHERE attrelid = %s::regclass AND attname = %s",
            (table, table, col),
        )[0][0] or ""

    close_c = col_comment("price_bars_daily", "close")
    assert "15:59" in close_c and "NOT the official" in close_c

    adj_c = col_comment("price_bars_daily", "adj_close")
    assert "NEVER a marking price" in adj_c
    assert "split_factor_between" in adj_c  # the decision-time-bounded path is named

    ppp_c = q(db, "SELECT obj_description('paper_portfolio_positions'::regclass)")[0][0] or ""
    # The documented marking formula must be raw × raw — the old text instructed
    # Σ shares × adj_close, which mis-marks a pre-split lot by the split factor (40× for NVDA).
    assert "RAW close" in ppp_c and "NEVER mark" in ppp_c


def test_bs4_daily_close_is_the_1559_bar_by_design(db: str, tmp_path: Path) -> None:
    """Behaviour pin: the 16:00 bucket (closing auction + post-close prints) stays EXCLUDED.
    The review explicitly warns against silently moving the bound to 16:00 — that bucket's close
    is a post-close print, a different wrong number. This test makes that change loud."""
    d = date(2025, 4, 9)
    # ABSOLUTE wall-clock anchors, deliberately NOT derived from session_bounds_ns/
    # SESSION_LAST_MINUTE — a pin computed from the constant under test would move with it.
    et = ldb.EXCHANGE_TZ
    lo_ns = int(datetime(2025, 4, 9, 9, 30, tzinfo=et).timestamp() * 1_000_000_000)
    ns_1559 = int(datetime(2025, 4, 9, 15, 59, tzinfo=et).timestamp() * 1_000_000_000)
    ns_1600 = int(datetime(2025, 4, 9, 16, 0, tzinfo=et).timestamp() * 1_000_000_000)
    root = tmp_path / "a"
    root.mkdir()
    path = root / f"{d.isoformat()}.csv.gz"
    with gzip.open(path, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(ldb.EXPECTED_HEADER)
        w.writerows([
            ["SPY", "1000", "543.50", "543.59", "543.60", "543.40", str(lo_ns), "9"],
            ["SPY", "4637786", "543.59", "543.37", "544.56", "542.66", str(ns_1559), "9"],
            ["SPY", "1224688", "543.35", "544.30", "548.62", "541.80", str(ns_1600), "9"],
        ])

    assert ldb.main(["--root", str(root)]) == ldb.EXIT_OK
    rows = q(db, "SELECT close, high, volume FROM price_bars_daily")
    assert rows == [(Decimal("543.37"), Decimal("544.56"), 4638786)]
    # The 16:00 bucket contributed nothing: not its close (544.30), not its high (548.62 — the
    # actual official auction print that day), not its 1,224,688 shares.


# ── B-S5: recycled tickers are split; dead names are marked dead ──────────────────────────────
def test_bs5_splice_and_infer(db: str) -> None:
    assert lrd.main(["calendar", "--from", "2021-01-01", "--to", "2025-12-31"]) == lrd.EXIT_OK

    fly = _seed_security(db, "FLY")
    _seed_bars(db, fly, {
        date(2021, 7, 30): "17.10", date(2021, 8, 2): "17.03",       # the old issuer…
        date(2025, 8, 7): "60.51", date(2025, 8, 8): "61.00",        # …and, years later, the new one
    })
    q(  # the CURRENT issuer's split, wrongly attached to the combined identity
        db,
        "INSERT INTO corporate_actions (security_id, action_type, ex_date, split_ratio) "
        "VALUES (%s, 'split', '2025-08-08', 2.0)",
        (fly,),
    )
    alive = _seed_security(db, "ALIVE")
    _seed_bars(db, alive, {date(2021, 8, 2): "10.00", date(2025, 8, 8): "12.00"})
    # ALIVE has a multi-year hole too — but bars through archive end. It still gets spliced
    # (a >180-day hole is two identities for backtest purposes); its POST-gap identity stays live.
    dead = _seed_security(db, "DEADCO")
    _seed_bars(db, dead, {date(2021, 6, 2): "5.00", date(2021, 6, 3): "4.90"})

    assert ldel.main(["splice"]) == ldel.EXIT_OK

    fly_rows = q(
        db,
        "SELECT id, first_seen, delisted_at FROM securities WHERE symbol = 'FLY' ORDER BY id",
    )
    assert len(fly_rows) == 2
    (old_id, _old_seen, old_del), (new_id, new_seen, new_del) = fly_rows
    assert old_del == date(2021, 8, 3)          # dead the day after its last bar
    assert new_seen == date(2025, 8, 7) and new_del is None
    # Bars are partitioned at the gap: the +255% "single-session return" is now unrepresentable.
    assert q(db, "SELECT count(*) FROM price_bars_daily WHERE security_id = %s", (old_id,))[0][0] == 2
    assert q(db, "SELECT count(*) FROM price_bars_daily WHERE security_id = %s", (new_id,))[0][0] == 2
    # The split follows the CURRENT holder — the wrong-adjustment-on-the-dead-issuer bug.
    assert q(db, "SELECT security_id FROM corporate_actions")[0][0] == new_id

    assert ldel.main(["infer"]) == ldel.EXIT_OK
    # DEADCO: no bar in the archive's final sessions → delisted the day after its last bar.
    assert q(db, "SELECT delisted_at FROM securities WHERE symbol = 'DEADCO'")[0][0] == date(2021, 6, 4)
    # The live holders of FLY and ALIVE trade through archive end → still live.
    assert q(
        db,
        "SELECT count(*) FROM securities WHERE delisted_at IS NULL AND symbol IN ('FLY', 'ALIVE')",
    )[0][0] == 2

    # And after adjust, the dead issuer's series is factor-1 while the live one is adjusted.
    assert lca.main(["adjust"]) == lca.EXIT_OK
    assert q(
        db,
        "SELECT DISTINCT split_adj_factor FROM price_bars_daily WHERE security_id = %s",
        (old_id,),
    ) == [(Decimal("1.000000000000"),)]
    assert q(
        db,
        "SELECT split_adj_factor FROM price_bars_daily WHERE security_id = %s AND trade_date = %s",
        (new_id, date(2025, 8, 7)),
    ) == [(Decimal("2.000000000000"),)]


# ── B-N1: provider comparison is adjusted-to-adjusted, and failures are collected ─────────────
def _mk_series(
    n: int, start: date, split_at: int | None, ratio: float
) -> tuple[list[tuple[date, float, float | None, float, int]], dict[date, tuple[float, int]]]:
    """A hand-built (ours, theirs) pair around an optional k:1 split at index `split_at`.

    `theirs` is constructed BY HAND on the provider's basis — every value written from the split
    arithmetic directly, never through the module under test — so the expectation cannot move
    with the code (the self-referential-pin trap the B-S4 first draft fell into). Raw closes
    drop by `ratio` at the split; the provider's split-adjusted series is continuous; the
    provider's volume is multiplied by `ratio` before the split (measured yfinance behaviour).
    Our session-window volume is set to 85% of official so check 6's band is exercised.
    """
    ours: list[tuple[date, float, float | None, float, int]] = []
    theirs: dict[date, tuple[float, int]] = {}
    for i in range(n):
        d = start + timedelta(days=i)
        adj = 100.0 + (i % 7) + i * 0.01          # non-degenerate, deterministic
        if split_at is not None and i < split_at:  # pre-split: raw price is ratio× the adjusted
            raw, factor = adj * ratio, ratio
            official_vol = 1000 * ratio            # provider scales pre-split volume by ratio
        else:
            raw, factor = adj, 1.0
            official_vol = 1000
        ours.append((d, raw, adj, factor, int(official_vol * 0.85 / factor)))
        theirs[d] = (adj, int(official_vol))
    return ours, theirs


def test_bn1_provider_comparison_is_adjusted_to_adjusted() -> None:
    """The original --provider run compared our RAW close to the provider's split-adjusted
    close and exited 1 on a CORRECT database (NVDA: 390,813 bps of pure basis mismatch),
    which also left checks 3/4/6 unreachable for every in-window split name. Like must be
    compared with like."""
    ours, theirs = _mk_series(300, date(2024, 1, 2), split_at=150, ratio=2.0)
    failures = vds._check_one_symbol("SPLITCO", ours, theirs, residual=1.0)
    # A 2:1 in-window split with a correct adjustment: zero failures. Under the reverted
    # raw-close binding, check 2 reports ~10,000 bps (raw 2× the provider's adjusted level for
    # 150 sessions) — the exact class of red-light-on-healthy-data this pins against.
    assert failures == []

    # NULL adj_close bars (factor-only levels) are skipped, not compared as zeros.
    ours_null = [(d, c, None if i < 10 else a, f, v) for i, (d, c, a, f, v) in enumerate(ours)]
    assert vds._check_one_symbol("SPLITCO", ours_null, theirs, residual=1.0) == []

    # A post-as-of split the provider has already applied: the residual re-bases their series.
    # Hand-built: provider halves every close and doubles every volume (its current basis after
    # a 2:1 split dated after our as-of); residual = 2 restores the as-of basis.
    theirs_post = {d: (c / 2.0, v * 2) for d, (c, v) in theirs.items()}
    assert vds._check_one_symbol("SPLITCO", ours, theirs_post, residual=2.0) == []
    # Without the residual the same series is a 100% level error — proof the re-basing is load-
    # bearing and a missing post-as-of split in our actions table cannot pass silently.
    assert any("check 2" in f for f in vds._check_one_symbol("SPLITCO", ours, theirs_post, residual=1.0))


def test_bn1_provider_failures_are_collected_not_raised(
    db: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The provider loop must check every symbol and report every failure: the first draft
    raised on the first failing symbol, so NVDA's basis bug hid the state of the other 14."""
    start = date(2024, 1, 2)
    series: dict[str, tuple] = {}
    for symbol, official_scale, vol_scale in (("BADCO", 0.5, 2.0), ("GOODCO", 1.0, 1.0)):
        sec = _seed_security(db, symbol)
        ours, theirs = _mk_series(300, start, split_at=None, ratio=1.0)
        with psycopg.connect(db, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO price_bars_daily (security_id, trade_date, open, high, low, "
                    "close, volume, adj_close, split_adj_factor) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [(sec, d, c, c, c, c, v, a, f) for d, c, a, f, v in ours],
                )
        # BADCO's "provider" disagrees by a constant factor (a basis error): check 2 must fail
        # on the level while check 3 passes (a constant scale cancels in returns) — exactly the
        # signature that distinguishes a basis bug from a data bug. Its volume is off band too,
        # so ONE symbol carries TWO findings and both must surface.
        series[symbol] = {
            d: (c * official_scale, int(v_official * vol_scale))
            for d, (c, v_official) in theirs.items()
        }
    q(db, "INSERT INTO price_adjustment_state (id, adjustment_as_of) VALUES (1, %s)",
      (start + timedelta(days=299),))

    class _Hist:
        def __init__(self, rows: dict):
            self._rows = rows

        def iterrows(self):
            class _TS:
                def __init__(self, d: date):
                    self._d = d

                def date(self) -> date:
                    return self._d

            for d, (c, v) in sorted(self._rows.items()):
                yield _TS(d), {"Close": c, "Volume": v}

    class _Ticker:
        def __init__(self, symbol: str):
            self._symbol = symbol

        def history(self, **_kw):
            return _Hist(series[self._symbol])

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))
    with caplog.at_level(logging.INFO):
        failures = vds.check_provider(psycopg.connect(db, autocommit=True), ("BADCO", "GOODCO"))
    # BADCO fails level and volume; the loop did NOT stop there — GOODCO was still checked and
    # passed. No failure mentions GOODCO.
    assert sum("BADCO" in f and "check 2" in f for f in failures) == 1
    assert sum("BADCO" in f and "check 6" in f for f in failures) == 1
    assert not any("GOODCO" in f for f in failures)
    assert any("provider checks PASS GOODCO" in r.message for r in caplog.records)


# ── B-N2: holes are classified by evidence and spliced from the audit, not a guessed length ───
def test_bn2_gap_audit_evidence_and_splice(db: str) -> None:
    assert lrd.main(["calendar", "--from", "2024-01-01", "--to", "2024-12-31"]) == lrd.EXIT_OK
    days = [r[0] for r in q(
        db,
        "SELECT trade_date FROM market_calendar WHERE is_trading_day "
        "AND trade_date BETWEEN '2024-01-15' AND '2024-06-28' ORDER BY trade_date",
    )]
    assert len(days) > 80
    # FILLER trades every session, so the others' holes are THEIR absences (missed COVERED
    # sessions), not archive-wide gaps.
    filler = _seed_security(db, "FILLER")
    _seed_bars(db, filler, {d: "20.00" for d in days})

    pre, post = days[5:10], days[70:73]           # a hole of 60 covered sessions
    recyc = _seed_security(db, "RECYC")           # 10.00 → 50.00: 5× no action explains
    _seed_bars(db, recyc, {**{d: "10.00" for d in pre}, **{d: "50.00" for d in post}})
    halty = _seed_security(db, "HALTY")           # 10.00 → 12.00: in-band, halt-consistent
    _seed_bars(db, halty, {**{d: "10.00" for d in pre}, **{d: "12.00" for d in post}})
    splitgap = _seed_security(db, "SPLITGAP")     # 1.00 → 9.50 across a recorded 1-for-10:
    _seed_bars(db, splitgap, {**{d: "1.00" for d in pre}, **{d: "9.50" for d in post}})
    q(db, "INSERT INTO corporate_actions (security_id, action_type, ex_date, split_ratio) "
          "VALUES (%s, 'split', %s, 0.1)", (splitgap, days[40]))

    assert ldel.main(["audit"]) == ldel.EXIT_OK
    disp = dict(q(db, "SELECT symbol, disposition FROM price_gap_audit"))
    # Evidence, not gap length, decides: all three holes are the same 60 sessions, and only the
    # unexplained discontinuity is flagged. SPLITGAP's ratio is measured NET of its recorded
    # split (9.5 × 0.1 = 0.95, in-band) — the SF-3 blind spot (a split INSIDE a hole) closed.
    assert disp == {"RECYC": "pending_review", "HALTY": "halt_consistent",
                    "SPLITGAP": "halt_consistent"}
    assert q(db, "SELECT adj_ratio, missed_sessions FROM price_gap_audit WHERE symbol = 'RECYC'") == [
        (Decimal("5.00000000"), 60)
    ]

    # The tripwire: an unresolved out-of-band hole FAILS verification — it cannot sit silently
    # in the return series the way the 448-security cohort did.
    with psycopg.connect(db, autocommit=True) as conn:
        with pytest.raises(vds.CheckFailure, match="unresolved"):
            vds.check_gap_audit(conn)

    # Default from-audit splice takes identity_break/provider_unresolvable only; pending rows
    # need provider evidence or the explicit no-egress flag.
    assert ldel.main(["splice", "--from-audit"]) == ldel.EXIT_OK
    assert q(db, "SELECT count(*) FROM securities WHERE symbol = 'RECYC'")[0][0] == 1
    assert ldel.main(["splice", "--from-audit", "--include-pending"]) == ldel.EXIT_OK

    rows = q(db, "SELECT id, delisted_at FROM securities WHERE symbol = 'RECYC' ORDER BY id")
    assert len(rows) == 2
    assert rows[0][1] == pre[-1] + timedelta(days=1)      # pre-gap issuer dead at the gap
    assert rows[1][1] is None
    # The fabricated +400% cross-gap "return" is now unrepresentable, the audit row says so,
    # and the in-band holes were left alone (splicing them would have been the old threshold
    # behaviour wearing a new name).
    assert q(db, "SELECT disposition FROM price_gap_audit WHERE symbol = 'RECYC'") == [("spliced",)]
    assert q(db, "SELECT count(*) FROM securities WHERE symbol IN ('HALTY', 'SPLITGAP')")[0][0] == 2

    with psycopg.connect(db, autocommit=True) as conn:
        vds.check_gap_audit(conn)                          # resolved: no raise

    # A NEW out-of-band hole appearing after the audit (fresh data) is loud, not grandfathered.
    unseen = _seed_security(db, "UNSEEN")
    _seed_bars(db, unseen, {**{d: "8.00" for d in pre}, **{d: "40.00" for d in post}})
    with psycopg.connect(db, autocommit=True) as conn:
        with pytest.raises(vds.CheckFailure, match="NO price_gap_audit row"):
            vds.check_gap_audit(conn)
