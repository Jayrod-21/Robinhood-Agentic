"""F5 regression: upstream exception text must never reach the client event stream.

The debate engine talks to the Anthropic SDK, whose exceptions stringify with the upstream status
code and response body, and its own ``DebateUnavailable`` used to name the on-disk config path.
These tests plant unmistakable markers in the raised exceptions and assert the client-visible
events carry only fixed generic messages — while the full detail still lands in the server log.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from app.debate import anthropic_client as ac
from app.debate import engine as engine_mod
from app.main import configure_logging

# Install the secret-redaction filter on the root handler (issue #14) so this module behaves the
# same run alone as in the full suite: the sk-ant token below must be scrubbed even server-side.
configure_logging()

# A string that could only appear client-side if str(exc) leaked into the stream. The sk-ant tail
# doubles as a redaction probe: the marker's non-secret half must reach the server log, the
# key-shaped half must not.
UPSTREAM_MARKER = "SECRET-UPSTREAM-RESPONSE-BODY-401-sk-ant-XXXX"


def _collect_events(ticker: str = "NVDA") -> list[dict]:
    """Drive run_debate to completion and return every yielded event."""

    async def _run() -> list[dict]:
        return [ev async for ev in engine_mod.run_debate(ticker)]

    return asyncio.run(_run())


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Keep the engine off yfinance: context fetch returns empty, harmlessly."""
    monkeypatch.setattr(engine_mod, "_fetch_context", lambda _t: (None, None))


def test_upstream_exception_text_not_streamed(monkeypatch, caplog):
    """A researcher-stage SDK failure yields a generic error event; str(exc) stays server-side."""

    async def _boom(*_a, **_k):
        raise RuntimeError(UPSTREAM_MARKER)

    monkeypatch.setattr(ac, "write_case", _boom)
    with caplog.at_level(logging.ERROR, logger="agentic.debate.engine"):
        events = _collect_events()

    errors = [ev for ev in events if ev["type"] == "error"]
    assert len(errors) == 1
    # The marker must not appear ANYWHERE in what the client would receive.
    assert all(UPSTREAM_MARKER not in str(ev) for ev in events)
    assert errors[0]["message"] == "Researcher stage failed upstream. Details are in the server logs."
    # ...but the server log keeps the debugging detail (traceback included via logger.exception) —
    # minus the key-shaped token, which the SecretRedactionFilter scrubs even server-side (#14).
    assert "SECRET-UPSTREAM-RESPONSE-BODY-401" in caplog.text
    assert "sk-ant-XXXX" not in caplog.text


def test_debate_unavailable_message_not_streamed(monkeypatch, caplog):
    """Even our own DebateUnavailable text is not forwarded — the stream gets a fixed message."""
    planted = "ANTHROPIC_API_KEY missing, put it in backend/.env"

    async def _unavailable(*_a, **_k):
        raise ac.DebateUnavailable(planted)

    monkeypatch.setattr(ac, "write_case", _unavailable)
    with caplog.at_level(logging.ERROR, logger="agentic.debate.engine"):
        events = _collect_events()

    errors = [ev for ev in events if ev["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["message"] == "Live debate engine is not configured on the server."
    assert all("backend/.env" not in str(ev) for ev in events)
    assert planted in caplog.text  # detail preserved server-side


def test_debate_unavailable_names_no_paths(monkeypatch, caplog):
    """_client()'s missing-key error is client-safe: no env-var name, no on-disk path. The
    remediation detail is logged server-side instead."""
    # _client() no longer reads settings — it asks app.llm.keys which credentials are configured,
    # so that a call can be attributed to whichever owner's key paid for it. The PROPERTY under
    # test is unchanged: no key is configured, and the client-facing message must not name an
    # env var or a path.
    from app.llm import keys as key_registry

    monkeypatch.setattr(key_registry, "available", lambda _provider=None: [])
    with caplog.at_level(logging.ERROR, logger="agentic.debate.anthropic"):
        with pytest.raises(ac.DebateUnavailable) as exc:
            ac._client()

    message = str(exc.value)
    assert ".env" not in message
    assert "ANTHROPIC_API_KEY" not in message
    assert "not configured" in message  # still actionable for the user
    assert any("ANTHROPIC_API_KEY" in rec.getMessage() for rec in caplog.records)
