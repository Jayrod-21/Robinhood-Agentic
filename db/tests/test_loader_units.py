"""Unit tests for the loaders' pure logic — no database, no network (loaders review S-7).

These guard a multi-hour irreplaceable load with millisecond tests: calendar rules against NYSE
reality, session-bound arithmetic across DST, the DayBar fold (out-of-order arrival, duplicate
buckets, the string money path), corrupt-stream classification, FRED parsing, and the retry /
provider-error contracts.
"""

from __future__ import annotations

import csv
import gzip
import sys
import zlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import load_corporate_actions as lca
import load_daily_bars as ldb
import load_minute_bars as lmb
import load_reference_data as lrd
import pytest


# ── NYSE calendar rules (verified against exchange reality 2020-2026) ─────────────────────────
def test_nyse_holidays_known_years() -> None:
    # Juneteenth: NYSE holiday from 2022, not before.
    assert date(2021, 6, 18) not in lrd.nyse_holidays(2021)
    assert date(2022, 6, 20) in lrd.nyse_holidays(2022)  # 2022-06-19 is a Sunday → Monday
    # The Carter closure is an ad-hoc closure folded into the single authority.
    assert date(2025, 1, 9) in lrd.nyse_holidays(2025)
    # 2020: July 4 falls Saturday → observed Friday 2020-07-03.
    assert date(2020, 7, 3) in lrd.nyse_holidays(2020)


def test_the_calendar_horizon_rolls_rather_than_expiring() -> None:
    """--to must be a date computed from today, not a literal.

    It was "2026-12-31" — comfortably far off when written, and weeks away by August 2026. The
    marking job refuses any date market_calendar does not know, so the equity curve was going to
    stop dead on 1 January with an error naming the calendar, and nothing would have said so in
    advance. A hardcoded horizon does not fail when it is set; it fails silently, later, to whoever
    is on shift.
    """
    horizon = date.fromisoformat(lrd.build_parser().parse_args(["calendar"]).date_to)
    assert horizon > date.today() + timedelta(days=365), (
        f"the default calendar horizon is {horizon}, under a year out — it is drifting toward "
        "expiry again"
    )


def test_holiday_rules_still_hold_years_ahead() -> None:
    """The horizon above is only safe because the holiday set is COMPUTED, not fetched. These are
    years no one has hand-checked, so the rules are pinned rather than assumed: weekend observance,
    a computed Good Friday, and an nth-weekday rule."""
    # Christmas 2027 is a Saturday, so the NYSE observes it on Friday the 24th.
    assert date(2027, 12, 24) in lrd.nyse_holidays(2027)
    # Easter 2028 is 16 April, putting Good Friday on the 14th.
    assert date(2028, 4, 14) in lrd.nyse_holidays(2028)
    # Thanksgiving is the fourth Thursday of November.
    assert date(2029, 11, 22) in lrd.nyse_holidays(2029)


def test_new_years_saturday_non_observance() -> None:
    """2022-01-01 is a Saturday; the NYSE does NOT shift it to Friday 2021-12-31 — the archive
    holds 10,871 bars for that session, and the general rule would have marked it closed."""
    hs = lrd.nyse_holidays(2022)
    assert date(2021, 12, 31) not in hs
    assert date(2022, 1, 1) not in {d for d in hs if d.weekday() < 5}  # never a weekday holiday


def test_nyse_early_closes() -> None:
    ec_2023 = lrd.nyse_early_closes(2023)
    assert date(2023, 7, 3) in ec_2023          # July 4 falls Tuesday
    assert date(2023, 11, 24) in ec_2023        # day after Thanksgiving
    ec_2024 = lrd.nyse_early_closes(2024)
    assert date(2024, 12, 24) in ec_2024        # Christmas falls Wednesday
    # 2026: July 4 falls Saturday (observed Friday) → NO July-3 early close (2020 precedent).
    assert date(2026, 7, 3) not in lrd.nyse_early_closes(2026)


# ── session bounds across DST ─────────────────────────────────────────────────────────────────
def test_session_bounds_follow_dst() -> None:
    """09:30 ET is 13:30 UTC in summer (EDT) and 14:30 UTC in winter (EST)."""
    summer_lo, summer_hi = ldb.session_bounds_ns(date(2024, 7, 5))
    winter_lo, winter_hi = ldb.session_bounds_ns(date(2024, 1, 5))
    assert summer_lo == int(datetime(2024, 7, 5, 13, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    assert winter_lo == int(datetime(2024, 1, 5, 14, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    # The window is inclusive of the 15:59 bar (389 minutes after the open).
    assert summer_hi - summer_lo == 389 * 60 * 1_000_000_000
    assert winter_hi - winter_lo == 389 * 60 * 1_000_000_000


# ── DayBar fold ───────────────────────────────────────────────────────────────────────────────
def _bar(ns: int) -> ldb.DayBar:
    return ldb.DayBar(ns, ns, "10.0", 11.0, 9.0, "11.0", "9.0", "10.5", 100, 1)


def test_daybar_out_of_order_open_close() -> None:
    b = _bar(5_000)
    b.update(1_000, "9.5", 10.0, "10.0", 9.4, "9.4", "9.9", 50)   # EARLIER row arrives later
    assert b.open == "9.5" and b.close == "10.5"                   # open follows earliest ns
    b.update(9_000, "10.6", 12.0, "12.0", 10.2, "10.2", "11.9", 25)
    assert b.close == "11.9" and b.high == 12.0 and b.high_s == "12.0"
    assert b.low == 9.0 and b.low_s == "9.0"
    assert b.volume == 175


def test_daybar_money_path_is_source_strings() -> None:
    """The value written for high/low is the provider's own decimal text, not a float repr —
    a 17-significant-digit source survives verbatim where float round-trip would mangle it."""
    precise = "655.12345678901234567"
    b = ldb.DayBar(1, 1, "655.0", float(precise), 600.0, precise, "600.0", "650.0", 10, 1)
    assert b.high_s == precise
    assert Decimal(b.high_s) == Decimal(precise)  # exact — no float in the stored path


def test_aggregate_file_skips_duplicate_window_start(tmp_path: Path) -> None:
    """Two rows sharing one (symbol, window_start) must not double-count volume (loaders N-8)."""
    d = date(2024, 7, 5)
    ns = ldb.session_bounds_ns(d)[0]  # 09:30 bar
    rows = [
        ["AAPL", "100", "10.0", "10.5", "11.0", "9.0", str(ns), "5"],
        ["AAPL", "100", "10.0", "10.5", "11.0", "9.0", str(ns), "5"],  # exact duplicate bucket
        ["AAPL", "200", "10.5", "10.7", "10.8", "10.4", str(ns + 60_000_000_000), "5"],
    ]
    path = tmp_path / f"{d.isoformat()}.csv.gz"
    with gzip.open(path, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(ldb.EXPECTED_HEADER)
        w.writerows(rows)
    bars, rows_read, skipped = ldb.aggregate_file(path)
    assert rows_read == 3 and skipped == 1
    assert bars[("AAPL", d)].volume == 300  # 100 + 200, duplicate NOT folded in


# ── corrupt-stream classification ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "exc",
    [
        zlib.error("invalid block type"),
        gzip.BadGzipFile("Not a gzipped file"),
        EOFError("Compressed file ended before the end-of-stream marker was reached"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        csv.Error("field larger than field limit (131072)"),
    ],
)
def test_rows_or_corrupt_classifies_every_stream_failure(exc: Exception) -> None:
    """S-1: the catch tuple must cover UnicodeDecodeError (a ValueError subclass gzip's text mode
    raises before the CRC check) and csv.Error (inflated garbage that decodes) — the original
    tuple missed both, turning them into run-aborting tracebacks."""
    def reader():
        yield ["ok", "row"]
        raise exc

    out = ldb._rows_or_corrupt(reader(), Path("2024-12-10.csv.gz"))
    assert next(out) == ["ok", "row"]
    with pytest.raises(ldb.CorruptArchive):
        next(out)

    out2 = lmb._rows_or_corrupt(reader(), Path("2024-12-10.csv.gz"))
    assert next(out2) == ["ok", "row"]
    with pytest.raises(lmb.CorruptArchive):
        next(out2)


def test_minute_loader_open_csv_classifies_bad_member(tmp_path: Path) -> None:
    """B-2: a not-actually-gzip member raises CorruptArchive, not gzip.BadGzipFile."""
    p = tmp_path / "2024-12-11.csv.gz"
    p.write_bytes(b"this is not a gzip stream at all")
    with pytest.raises(lmb.CorruptArchive):
        lmb.scan_file(p)


def test_symbol_grammar() -> None:
    for good in ("AAPL", "BRK.B", "BACpA", "TDW.WS.A", "AANw", "A"):
        assert lmb.SYMBOL_RE.match(good), good
    for bad in ("BAD SYM", ".SPX", "TOOLONGSYMBOL", "A" * 11, ""):
        assert not lmb.SYMBOL_RE.match(bad), bad


# ── FRED parsing and fetch retry ──────────────────────────────────────────────────────────────
def test_parse_fred_csv_decimal_and_skips() -> None:
    payload = (
        "observation_date,DGS3MO\n"
        "2024-01-01,.\n"          # non-publication day
        "2024-01-02,5.25\n"
        "2024-01-03,\n"           # empty
        "garbage,5.0\n"           # bad date
        "2024-01-04,1.53\n"
    )
    rows, skipped = lrd.parse_fred_csv(payload, series="DGS3MO")
    assert skipped == 3
    assert rows == [
        (date(2024, 1, 2), Decimal("0.0525")),
        (date(2024, 1, 4), Decimal("0.0153")),  # exactly — float would give 0.015300000000000001
    ]
    assert all(isinstance(r[1], Decimal) for r in rows)


def test_parse_fred_csv_rejects_unknown_columns() -> None:
    with pytest.raises(lrd.LoadError):
        lrd.parse_fred_csv("date,WRONG_SERIES\n2024-01-02,5.0\n", series="DGS3MO")


def test_fetch_fred_retries_then_raises_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """S-3: the GET retries with backoff and a final failure is FetchError (exit-3 channel),
    never a bare validation error."""
    import urllib.error

    calls = {"n": 0}

    def failing_urlopen(url, timeout):
        calls["n"] += 1
        raise urllib.error.URLError("temporarily down")

    sleeps: list[float] = []
    monkeypatch.setattr(lrd.urllib.request, "urlopen", failing_urlopen)
    with pytest.raises(lrd.FetchError):
        lrd.fetch_fred_csv("https://example.invalid/x", attempts=3, sleep=sleeps.append)
    assert calls["n"] == 3
    assert len(sleeps) == 2 and all(s > 0 for s in sleeps)


def test_fetch_fred_recovers_on_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    calls = {"n": 0}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"observation_date,DGS3MO\n2024-01-02,5.25\n"

    def flaky_urlopen(url, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("blip")
        return FakeResp()

    monkeypatch.setattr(lrd.urllib.request, "urlopen", flaky_urlopen)
    payload = lrd.fetch_fred_csv("https://example.invalid/x", attempts=3, sleep=lambda _s: None)
    assert "5.25" in payload


# ── provider-error contract (B-1's unit half) ─────────────────────────────────────────────────
def test_fetch_actions_raises_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-1: a provider failure must RAISE (typed), never return an empty 'no actions' result."""
    fake = ModuleType("yfinance")

    class ExplodingTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def splits(self):
            raise ConnectionResetError("rate limited")

        @property
        def dividends(self):  # pragma: no cover — splits raises first
            raise ConnectionResetError("rate limited")

    fake.Ticker = ExplodingTicker
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    with pytest.raises(lca.ProviderError):
        lca.fetch_actions("SPY")


def test_fetch_actions_returns_actions_and_filters_noop_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = ModuleType("yfinance")

    class Series:
        def __init__(self, items):
            self._items = items

        def items(self):
            return iter(self._items)

    class FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.splits = Series([
                (SimpleNamespace(date=lambda: date(2021, 7, 20)), 4.0),
                (SimpleNamespace(date=lambda: date(2022, 1, 5)), 1.0),   # no-op ratio filtered
            ])
            self.dividends = Series([
                (SimpleNamespace(date=lambda: date(2021, 8, 6)), 0.22),
            ])

    fake.Ticker = FakeTicker
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    splits, divs = lca.fetch_actions("NVDA")
    assert splits == [(date(2021, 7, 20), 4.0)]
    assert divs == [(date(2021, 8, 6), 0.22)]


# ── argparse hardening (N-3) ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("module", [lmb, ldb])
def test_limit_zero_is_rejected(module) -> None:
    with pytest.raises(SystemExit):
        module.build_parser().parse_args(["--limit", "0"])
    assert module.build_parser().parse_args(["--limit", "1"]).limit == 1
