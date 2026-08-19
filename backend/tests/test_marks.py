"""Live marks: per-symbol soft-fail and TTL caching, without touching the network."""

from app.services import marks


def test_soft_fail_returns_none(monkeypatch):
    monkeypatch.setattr(marks, "_CACHE", {})
    monkeypatch.setattr(marks, "_fetch_one", lambda sym: None)
    out = marks.get_marks(["FAKE"], ttl_seconds=30)
    assert out == {"FAKE": None}


def test_caches_within_ttl(monkeypatch):
    monkeypatch.setattr(marks, "_CACHE", {})
    calls = {"n": 0}

    def fake(sym):
        calls["n"] += 1
        return 100.0

    monkeypatch.setattr(marks, "_fetch_one", fake)
    marks.get_marks(["AAA"], ttl_seconds=60)
    marks.get_marks(["AAA"], ttl_seconds=60)  # should hit cache, not refetch
    assert calls["n"] == 1


def test_mixed_symbols(monkeypatch):
    monkeypatch.setattr(marks, "_CACHE", {})
    monkeypatch.setattr(marks, "_fetch_one", lambda sym: 50.0 if sym == "OK" else None)
    out = marks.get_marks(["OK", "BAD"], ttl_seconds=30)
    assert out["OK"] == 50.0 and out["BAD"] is None


# ── the outage of 2026-08-19 ──────────────────────────────────────────────────────────────────
#
# Positions went blank a few minutes after every restart and never recovered. Two causes, stacked:
# the daily FMP budget was sized for a backfill (250) while the dashboard spends ~20 calls a minute
# pricing fifteen positions, and — the real defect — a failed fetch OVERWROTE the cached price with
# None. The budget running out was survivable; the cache destroying what it knew was not.


def test_a_failed_fetch_never_destroys_the_last_known_price(monkeypatch):
    """The bug, in one test. A price we already have is worth more than the failure that stopped us
    refreshing it, and throwing it away is what turned an exhausted quota into a blank page."""
    from app.services import marks

    marks.reset_cache()
    monkeypatch.setattr(marks, "_fetch_one", lambda sym: 100.0)
    assert marks.get_marks(["AMD"], ttl_seconds=0)["AMD"] == 100.0

    # Now the provider fails — budget exhausted, network down, whatever.
    monkeypatch.setattr(marks, "_fetch_one", lambda sym: None)
    assert marks.get_marks(["AMD"], ttl_seconds=0)["AMD"] == 100.0, (
        "the last known price was discarded when the refresh failed"
    )
    marks.reset_cache()


def test_a_price_served_past_its_ttl_is_flagged_stale(monkeypatch):
    """Serving a stale price is right; serving it as if it were current is not. The caller has to be
    able to tell, or the page shows a ten-minute-old number as live."""
    from app.services import marks

    marks.reset_cache()
    monkeypatch.setattr(marks, "_fetch_one", lambda sym: 100.0)
    fresh = marks.get_marks_detailed(["AMD"], ttl_seconds=300)["AMD"]
    assert fresh.price == 100.0 and fresh.stale is False

    monkeypatch.setattr(marks, "_fetch_one", lambda sym: None)
    served = marks.get_marks_detailed(["AMD"], ttl_seconds=0)["AMD"]
    assert served.price == 100.0
    assert served.stale is True, "a fallback price must announce itself as one"
    marks.reset_cache()


def test_a_price_too_old_to_mean_anything_is_withheld(monkeypatch):
    """Stale has a limit. An hour-old mark is not a mark, and pricing a position on it would put a
    confident number on a fact that has expired."""
    from app.services import marks

    marks.reset_cache()
    monkeypatch.setattr(marks, "_fetch_one", lambda sym: 100.0)
    marks.get_marks(["AMD"], ttl_seconds=300)

    # Age the cached entry past the stale ceiling.
    with marks._LOCK:
        price, _ = marks._CACHE["AMD"]
        marks._CACHE["AMD"] = (price, marks.time.monotonic() - marks._MAX_STALE_SECONDS - 1)

    monkeypatch.setattr(marks, "_fetch_one", lambda sym: None)
    assert marks.get_marks_detailed(["AMD"], ttl_seconds=0)["AMD"].price is None
    marks.reset_cache()


def test_a_failing_symbol_is_not_retried_on_every_request(monkeypatch):
    """With the budget exhausted, every poll re-attempted all fifteen positions: fifteen guaranteed
    failures and fifteen log lines per poll, burying the one line that said what was wrong."""
    from app.services import marks

    marks.reset_cache()
    calls = []
    monkeypatch.setattr(marks, "_fetch_one", lambda sym: calls.append(sym) or None)

    for _ in range(5):
        marks.get_marks(["AMD"], ttl_seconds=0)

    assert len(calls) == 1, f"expected one attempt then backoff, got {len(calls)}"
    marks.reset_cache()


def test_a_recovered_provider_clears_the_backoff(monkeypatch):
    """The throttle must not outlive the failure — a provider that comes back should be used."""
    from app.services import marks

    marks.reset_cache()
    monkeypatch.setattr(marks, "_fetch_one", lambda sym: None)
    marks.get_marks(["AMD"], ttl_seconds=0)
    assert "AMD" in marks._FAILED_AT

    monkeypatch.setattr(marks, "_FAILED_AT", {})       # simulate the backoff window elapsing
    monkeypatch.setattr(marks, "_fetch_one", lambda sym: 42.0)
    assert marks.get_marks(["AMD"], ttl_seconds=0)["AMD"] == 42.0
    marks.reset_cache()
