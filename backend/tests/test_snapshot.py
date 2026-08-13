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


def _write(tmp_path, payload):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(payload))
    return path


@pytest.mark.parametrize("quantity", [-1.0, -0.0958, 0.0])
def test_nonpositive_quantity_rejected(tmp_path, quantity):
    """F13 regression: a listed position must be a strictly positive holding. A negative or zero
    quantity is fabricated/malformed data and must fail validation, not flow into P&L math."""
    bad = {**GOOD, "positions": [{**GOOD["positions"][0], "quantity": quantity}]}
    with pytest.raises(SnapshotError, match="validation"):
        load_snapshot(_write(tmp_path, bad))


@pytest.mark.parametrize("field", ["cash", "buying_power"])
def test_negative_cash_and_buying_power_rejected(tmp_path, field):
    """F13 regression: this is a cash account — negative cash/buying power is impossible input."""
    bad = {**GOOD, "account": {**GOOD["account"], field: -0.01}}
    with pytest.raises(SnapshotError, match="validation"):
        load_snapshot(_write(tmp_path, bad))


@pytest.mark.parametrize("version", [0, 2, 99])
def test_schema_version_mismatch_rejected_loudly(tmp_path, version, caplog):
    """F13 regression: schema_version is COMPARED, not just declared. Any value other than the
    supported version must refuse to load, and must say so in the server log (never silently)."""
    bad = {**GOOD, "schema_version": version}
    with pytest.raises(SnapshotError, match="schema_version") as exc:
        load_snapshot(_write(tmp_path, bad))
    assert str(version) in str(exc.value)  # the error names the offending version
    assert any(
        "schema_version" in rec.getMessage() and rec.levelname == "ERROR"
        for rec in caplog.records
    )


def test_supported_schema_version_still_loads(tmp_path):
    """The version gate must not reject the version the producer actually writes."""
    snap = load_snapshot(_write(tmp_path, GOOD))
    assert snap.schema_version == 1
