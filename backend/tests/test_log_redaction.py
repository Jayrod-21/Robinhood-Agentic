"""Log hygiene (issue #14): secret-redaction filter + holdings logged as counts, not tickers."""

import asyncio
import io
import logging
from types import SimpleNamespace

from app.jobs import cycle as cycle_mod
from app.main import SecretRedactionFilter, configure_logging


def _emit(msg, *args, exc=None):
    """Send one record through a handler carrying the redaction filter; return the emitted text."""
    logger = logging.getLogger("redaction-test")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter())
    logger.addHandler(handler)
    try:
        if exc is not None:
            try:
                raise exc
            except Exception:
                logger.exception(msg, *args)
        else:
            logger.info(msg, *args)
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def test_anthropic_key_redacted():
    out = _emit("client init failed with key %s", "sk-ant-api03-abc123XYZ")
    assert "abc123XYZ" not in out
    assert "sk-ant-[REDACTED]" in out


def test_authorization_header_redacted():
    out = _emit("request headers: authorization: Bearer tok_abc.def123")
    assert "tok_abc.def123" not in out
    assert "[REDACTED]" in out


def test_api_key_assignment_redacted():
    out = _emit("retrying with api_key=abcd1234efgh")
    assert "abcd1234efgh" not in out
    assert "[REDACTED]" in out


def test_otpauth_uri_redacted():
    # AUTH_THREAT_MODEL §5.4: a provisioning URI embeds the TOTP shared secret as a query
    # parameter — one logged URI and the second factor is reproducible forever. The WHOLE URI
    # must be destroyed (label, issuer, and secret= together), not just the parameter.
    out = _emit(
        "enrollment response: otpauth://totp/Agentic:op@example.com"
        "?secret=JBSWY3DPEHPK3PXP&issuer=Agentic"
    )
    assert "JBSWY3DPEHPK3PXP" not in out
    assert "secret=" not in out
    assert "otpauth://[REDACTED]" in out


def test_totp_secret_redacted_in_exception_text():
    # §5.4's second net: a BARE base32 secret (no otpauth:// context) landing in an exception —
    # e.g. a crypto.py or manage_operator.py bug embedding it in the message — must be scrubbed
    # from the rendered traceback while the traceback itself survives.
    out = _emit(
        "enrollment failed",
        exc=RuntimeError("decrypt round-trip mismatch for secret GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"),
    )
    assert "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ" not in out
    assert "[REDACTED-BASE32]" in out
    assert "RuntimeError" in out  # the traceback itself still gets emitted


def test_exception_traceback_redacted():
    # The structural gap from the finding: an SDK exception embedding an auth header would land in
    # logger.exception output. The traceback must survive, the secret must not.
    out = _emit("call failed", exc=RuntimeError("401 from api; x-api-key: hunter2secret"))
    assert "hunter2secret" not in out
    assert "RuntimeError" in out  # the traceback itself still gets emitted


def test_clean_records_pass_through_unchanged():
    out = _emit("debating %d position(s)", 2)
    assert "debating 2 position(s)" in out


def test_configure_logging_installs_filter_once():
    configure_logging()
    configure_logging()  # idempotent: a second call must not stack duplicate filters
    handlers = logging.getLogger().handlers
    assert handlers, "configure_logging must leave the root logger with at least one handler"
    for handler in handlers:
        assert sum(isinstance(f, SecretRedactionFilter) for f in handler.filters) == 1


def test_cycle_logs_position_count_not_symbols(tmp_path, monkeypatch, caplog):
    # cycle.py used to log ", ".join(symbols) — the account's holdings list — twice a day.
    settings = SimpleNamespace(
        anthropic_api_key="test-key",
        logs_dir=tmp_path / "logs",
        events_path=tmp_path / "logs" / "events.jsonl",
    )
    account = SimpleNamespace(
        positions=[SimpleNamespace(symbol="ZZTOPA"), SimpleNamespace(symbol="QQBETA")],
        live_total_value=200.0,
        live_equity_value=160.0,
        cash=40.0,
        total_unrealized_pl=5.0,
        total_unrealized_pl_pct=2.5,
        generated_at="2026-08-13T00:00:00Z",
        stale_prices=False,
    )
    monkeypatch.setattr(cycle_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(cycle_mod, "_account_view_sync", lambda: account)
    monkeypatch.setattr(cycle_mod, "_run_scan_sync", list)  # no survivors, no network

    async def fake_debate(ticker, sem):
        return {"ticker": ticker, "decision": "HOLD", "escalated": False, "reason": None, "error": None}

    monkeypatch.setattr(cycle_mod, "_run_one_debate", fake_debate)

    with caplog.at_level(logging.INFO, logger="agentic.jobs.cycle"):
        asyncio.run(cycle_mod.run_cycle("close"))

    assert "2 position(s)" in caplog.text
    assert "ZZTOPA" not in caplog.text
    assert "QQBETA" not in caplog.text
