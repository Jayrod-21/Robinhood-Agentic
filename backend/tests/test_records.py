"""Debate record helpers: archive date/title parsing, traversal safety, round-trip persistence."""

import pytest

from app.debate import records
from app.debate.records import _date_from_stem, _first_heading, get_record
from app.debate.schemas import DebateRecord, Decision


def test_date_from_stem():
    assert _date_from_stem("2026-06-03-debate-1-best-path") == "2026-06-03T00:00:00Z"
    assert _date_from_stem("no-date-here") == "1970-01-01T00:00:00Z"


def test_first_heading():
    md = "intro line\n# Debate 2 — Allocation\n\nbody"
    assert _first_heading(md) == "Debate 2 — Allocation"
    assert _first_heading("no headings at all") == "(untitled debate)"


def test_list_records_includes_archive_markdown(tmp_path, monkeypatch):
    debates = tmp_path / "debates"
    debates.mkdir()
    (debates / "2026-06-03-debate-9-demo.md").write_text("# Demo debate\n\nbody")

    class FakeSettings:
        debates_dir = debates

    monkeypatch.setattr(records, "get_settings", lambda: FakeSettings())
    out = records.list_records()
    assert len(out) == 1
    assert out[0]["source"] == "archive"
    assert out[0]["question"] == "Demo debate"
    assert out[0]["created_at"] == "2026-06-03T00:00:00Z"


def _patch_debates_dir(tmp_path, monkeypatch):
    debates = tmp_path / "debates"
    debates.mkdir()

    class FakeSettings:
        debates_dir = debates

    monkeypatch.setattr(records, "get_settings", lambda: FakeSettings())
    return debates


def test_get_record_round_trip(tmp_path, monkeypatch):
    """A persisted engine record reads back through get_record by its id."""
    debates = _patch_debates_dir(tmp_path, monkeypatch)
    rec = DebateRecord(
        id="2026-06-16-engine-NVDA",
        ticker="NVDA",
        created_at="2026-06-16T00:00:00Z",
        question="Buy NVDA?",
        final_decision=Decision.HOLD,
    )
    (debates / f"{rec.id}.json").write_text(rec.model_dump_json(indent=2))

    out = get_record(rec.id)
    assert out is not None
    assert out["id"] == rec.id
    assert out["ticker"] == "NVDA"
    assert out["final_decision"] == "HOLD"


def test_get_record_reads_archive_markdown(tmp_path, monkeypatch):
    debates = _patch_debates_dir(tmp_path, monkeypatch)
    (debates / "2026-06-03-archive.md").write_text("# Archive\n\nbody")
    out = get_record("2026-06-03-archive")
    assert out is not None
    assert out["source"] == "archive"
    assert "Archive" in out["markdown"]


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",          # classic traversal
        "..%2F..%2Fetc%2Fpasswd",   # encoded-looking; the decoded ".." must be rejected
        "../config",
        "..",
        "foo/bar",                   # path separator
        "foo\\bar",                  # windows separator
        "a/../../b",
    ],
)
def test_get_record_rejects_traversal_ids(tmp_path, monkeypatch, bad_id):
    """B1: a traversal/encoded id must return None (no file read), not escape the debates dir.

    We plant a secret .json OUTSIDE the debates dir; get_record must never reach it.
    """
    debates = _patch_debates_dir(tmp_path, monkeypatch)
    secret = tmp_path / "secret.json"
    secret.write_text('{"top": "secret"}')
    # Also a sibling at the parent that a "../secret" style id would target.
    (debates.parent / "secret.json").write_text('{"top": "secret"}')

    assert get_record(bad_id) is None


def test_get_record_missing_id_returns_none(tmp_path, monkeypatch):
    _patch_debates_dir(tmp_path, monkeypatch)
    assert get_record("does-not-exist") is None
