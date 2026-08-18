"""Every endpoint payload carries the fields its TypeScript interface declares required.

WHY THIS FILE EXISTS
    /api/performance and /api/calibration shipped with shapes I invented rather than the ones in
    frontend/src/lib/*.ts, which both contracts name as the source of truth. `date` instead of
    `trade_date`, a parallel benchmark array instead of `benchmark_cumulative_return` on each point,
    `n` instead of `n_observations`. The request succeeded, the page read undefined everywhere, and
    it died with "a client-side exception has occurred".

    That is worse than a 404. A missing endpoint announces itself; a 200 with the wrong shape looks
    like working software right up until the render — and no backend test noticed, because every
    one of them asserted against the same wrong shape I had just written.

    So this reads the REAL TypeScript and fails when a payload stops satisfying it. It is the only
    test here that can catch the backend and the frontend drifting apart, because it is the only one
    that reads both.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib"


def required_fields(ts_file: str, interface: str) -> set[str]:
    """Field names an interface declares WITHOUT `?`. Optional ones may legitimately be absent."""
    text = (LIB / ts_file).read_text(encoding="utf-8")
    m = re.search(rf"export interface {interface} \{{(.*?)\n\}}", text, re.S)
    assert m, f"{interface} not found in {ts_file} — the interface was renamed or removed"
    out: set[str] = set()
    for line in m.group(1).splitlines():
        mm = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(\??):", line.strip())
        if mm and not mm.group(2):
            out.add(mm.group(1))
    assert out, f"parsed zero required fields from {interface} — the regex went blind"
    return out


def _perf_payload():
    from app.routers import performance as mod

    return mod._empty("no book in this test")


def test_performance_meta_matches_the_interface():
    payload = _perf_payload()
    missing = required_fields("perf.ts", "PerformanceMeta") - set(payload["meta"])
    assert not missing, f"PerformanceMeta is missing {sorted(missing)}"


def test_performance_response_keys_match():
    payload = _perf_payload()
    missing = required_fields("perf.ts", "PerformanceResponse") - set(payload)
    assert not missing, f"PerformanceResponse is missing {sorted(missing)}"


def _performance_with_marks(monkeypatch):
    """Drive the REAL endpoint with a stubbed database so the point it builds is the one asserted.

    An earlier version of this test wrote its own dict literal and compared THAT to the interface.
    It passed against a renamed key in the endpoint, because it never ran the endpoint — the exact
    shape of vacuous test this file exists to prevent, committed inside the file itself.
    """
    from app.routers import performance as mod

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return self._rows

    class _Conn:
        def execute(self, sql, params=None):
            if "FROM paper_portfolios" in sql:
                return _Cur([(1, "real", "2026-08-17")])
            if "portfolio_returns_daily" in sql:
                return _Cur([("2026-08-17", 100.0, None), ("2026-08-18", 101.0, 0.01)])
            if "price_bars_daily" in sql:
                return _Cur([("2026-08-17", 500.0), ("2026-08-18", 505.0)])
            if "evaluation_runs" in sql:
                return _Cur([])
            return _Cur([])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "connection", lambda: _Conn())
    return mod.performance()


def test_equity_point_matches_the_interface(monkeypatch):
    payload = _performance_with_marks(monkeypatch)
    assert payload["equity_curve"], "the stub should have produced marks"
    point = payload["equity_curve"][0]
    missing = required_fields("perf.ts", "EquityPoint") - set(point)
    assert not missing, f"EquityPoint is missing {sorted(missing)}"


def test_the_benchmark_rides_on_each_point_not_a_parallel_array(monkeypatch):
    """The chart reads one series of records. A separate benchmark array was the original bug."""
    payload = _performance_with_marks(monkeypatch)
    assert "benchmark" not in payload, "a parallel benchmark array is not the contract"
    assert payload["equity_curve"][-1]["benchmark_cumulative_return"] == pytest.approx(0.01)


def test_calibration_payload_matches_the_interface(monkeypatch):
    from app.routers import performance as mod

    class _Conn:
        def execute(self, *a, **k):
            class R:
                def fetchone(self_inner):
                    return (0,)
            return R()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(mod, "connection", lambda: _Conn())
    payload = mod.calibration(scope="jury")

    for iface, obj in (
        ("CalibrationResponse", payload),
        ("CalibrationMeta", payload["meta"]),
        ("CalibrationSummary", payload["overall"]),
        ("CalibrationBin", payload["overall"]["bins"][0]),
    ):
        missing = required_fields("calibration.ts", iface) - set(obj)
        assert not missing, f"{iface} is missing {sorted(missing)}"


def test_every_bin_is_present_even_with_no_data():
    """A reliability diagram with no buckets collapses instead of drawing an empty grid."""
    from app.routers import performance as mod

    assert len(mod._BINS) == 10
    assert mod._BINS[0][0] == 0.0 and mod._BINS[-1][1] == 1.0


@pytest.mark.parametrize(
    "ts_file,interface",
    [
        ("reconciliation.ts", "ReconPosition"),
        ("reconciliation.ts", "ReconMeta"),
        ("reconciliation.ts", "ReconSummary"),
        ("dataTrust.ts", "DataTrustResponse"),
        ("reconciliation.ts", "ReconciliationResponse"),
        ("reconciliation.ts", "DisciplineCheck"),
    ],
)
def test_the_interfaces_the_other_endpoints_serve_are_parseable(ts_file, interface):
    """A guard on this file's own method: if the regex stops finding interfaces, every assertion
    above would pass vacuously against an empty required-set."""
    assert required_fields(ts_file, interface)


# ── fundamentals ──────────────────────────────────────────────────────────────────────────────
#
# The page reads every one of these by name across four field groups and a history table. A column
# renamed in the SELECT list would render the whole grid as em dashes — visually indistinguishable
# from a company with no data, which is the failure this file exists to catch.


def test_the_annual_select_list_covers_the_typescript_interface():
    from app.routers.fundamentals import _ANNUAL_FIELDS

    missing = required_fields("fundamentals.ts", "AnnualPeriod") - set(_ANNUAL_FIELDS)
    assert not missing, f"AnnualPeriod declares fields the query never selects: {sorted(missing)}"


def test_the_market_select_list_covers_the_typescript_interface():
    from app.routers.fundamentals import _MARKET_FIELDS

    missing = required_fields("fundamentals.ts", "MarketBlock") - set(_MARKET_FIELDS)
    assert not missing, f"MarketBlock declares fields the query never selects: {sorted(missing)}"


def test_every_field_the_page_renders_is_one_the_backend_selects():
    """The page drives its grid from ANNUAL_GROUPS / MARKET_FIELDS in fundamentals.ts. A label
    pointing at a key nothing selects renders an em dash forever and never errors."""
    text = (LIB / "fundamentals.ts").read_text(encoding="utf-8")
    from app.routers.fundamentals import _ANNUAL_FIELDS, _MARKET_FIELDS

    keys = set(re.findall(r'\{ key: "([a-z0-9_]+)"', text))
    assert len(keys) > 25, f"parsed only {len(keys)} field keys — the regex went blind"
    unknown = keys - set(_ANNUAL_FIELDS) - set(_MARKET_FIELDS)
    assert not unknown, f"the page renders keys the backend never returns: {sorted(unknown)}"


def test_the_piotroski_signal_labels_match_the_scorer():
    """The page labels nine signals by key. A key the scorer does not emit renders as a permanent
    'inputs missing' row — a real signal reported as unmeasurable."""
    text = (LIB / "fundamentals.ts").read_text(encoding="utf-8")
    m = re.search(r"export const PIOTROSKI_LABELS[^{]*\{(.*?)\n\};", text, re.S)
    assert m, "PIOTROSKI_LABELS not found"
    labelled = set(re.findall(r"^\s*([a-z_]+):", m.group(1), re.M))

    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from src.piotroski import SIGNAL_NAMES

    assert labelled == set(SIGNAL_NAMES), (
        f"page labels {sorted(labelled)} but the scorer emits {sorted(SIGNAL_NAMES)}"
    )
