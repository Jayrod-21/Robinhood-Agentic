"""Refresh trigger: atomic write (S1), cooldown + pending gates, and writable-failure handling."""

import json

import pytest
from fastapi import HTTPException

from app.routers import refresh as refresh_mod
from app.routers.refresh import _atomic_write, request_refresh


class _Settings:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.refresh_cooldown_seconds = 20
        self.agentic_account_masked = "••••4025"

    @property
    def refresh_request_path(self):
        return self.data_dir / "refresh.request"


@pytest.fixture(autouse=True)
def _reset_limiter():
    # The cooldown clock is a module global; reset around each test so they don't bleed.
    refresh_mod._last_request_monotonic = None
    yield
    refresh_mod._last_request_monotonic = None


def test_atomic_write_produces_complete_file_and_no_tmp(tmp_path):
    target = tmp_path / "refresh.request"
    _atomic_write(target, '{"x": 1}\n')
    assert json.loads(target.read_text()) == {"x": 1}
    # No leftover temp files in the directory.
    assert list(tmp_path.glob("refresh.request.*.tmp")) == []


def test_request_refresh_queues_atomically(tmp_path, monkeypatch):
    settings = _Settings(tmp_path)
    monkeypatch.setattr(refresh_mod, "get_settings", lambda: settings)

    res = request_refresh()
    assert res.status == "queued"
    body = json.loads(settings.refresh_request_path.read_text())
    assert body["account"] == "••••4025"
    assert "requested_at" in body
    assert list(tmp_path.glob("*.tmp")) == []


def test_pending_when_trigger_already_exists(tmp_path, monkeypatch):
    settings = _Settings(tmp_path)
    monkeypatch.setattr(refresh_mod, "get_settings", lambda: settings)
    settings.refresh_request_path.parent.mkdir(parents=True, exist_ok=True)
    settings.refresh_request_path.write_text("{}\n")

    res = request_refresh()
    assert res.status == "pending"


def test_cooldown_blocks_rapid_second_request(tmp_path, monkeypatch):
    settings = _Settings(tmp_path)
    monkeypatch.setattr(refresh_mod, "get_settings", lambda: settings)

    first = request_refresh()
    assert first.status == "queued"
    # Simulate the daemon consuming the trigger so we're past the pending gate.
    settings.refresh_request_path.unlink()
    second = request_refresh()
    assert second.status == "cooldown"
    assert second.cooldown_remaining_s > 0


def test_write_failure_surfaces_503(tmp_path, monkeypatch):
    settings = _Settings(tmp_path)
    monkeypatch.setattr(refresh_mod, "get_settings", lambda: settings)

    def boom(_path, _contents):
        raise OSError("read-only mount")

    monkeypatch.setattr(refresh_mod, "_atomic_write", boom)
    with pytest.raises(HTTPException) as exc:
        request_refresh()
    assert exc.value.status_code == 503
