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
