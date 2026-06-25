"""Snapshot loading + validation."""

import json

import pytest

from app.services.snapshot import SnapshotError, load_snapshot

GOOD = {
    "schema_version": 1,
    "source": "robinhood-mcp",
    "generated_at": "2026-06-16T17:18:47Z",
    "account": {"number_masked": "••••4025", "total_value": 198.4, "equity_value": 155.9, "cash": 42.5, "buying_power": 33.4},
    "positions": [{"symbol": "TSM", "quantity": 0.0958, "average_buy_price": 438.59, "intraday_quantity": 0.0}],
}


def test_loads_valid_snapshot(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(GOOD))
    snap = load_snapshot(path)
    assert snap.account.cash == 42.5
    assert snap.symbols == ["TSM"]


def test_missing_file_raises(tmp_path):
    with pytest.raises(SnapshotError):
        load_snapshot(tmp_path / "nope.json")


def test_malformed_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(SnapshotError):
        load_snapshot(path)


def test_schema_violation_raises(tmp_path):
    path = tmp_path / "wrong.json"
    bad = {**GOOD, "account": {"total_value": "abc"}}  # non-numeric, missing fields
    path.write_text(json.dumps(bad))
    with pytest.raises(SnapshotError):
        load_snapshot(path)
