"""Transactional email service (AUTH_THREAT_MODEL.md §5.6, §5.11).

What is pinned here, and why:
- transport selection: SMTP_HOST unset (or empty, the compose ``${VAR:-}`` case) must mean the
  log-only mock — dev and CI have no Bridge and must never open a socket;
- the verification link is absolute against the configured public origin and carries the token in
  the URL FRAGMENT, never the query string (§5.6);
- a send failure surfaces as a constant-message error with the chain severed — no credentials,
  hostnames, or internal paths in anything client-visible (the issue #13 class), and credentials
  are scrubbed from the server-side log line too;
- the raw token never reaches a log line on the real (SMTP) send path, and neither does the
  recipient's local part;
- STARTTLS precedes AUTH unconditionally, and certificate verification is on unless the operator
  explicitly opted out for the Bridge's self-signed cert;
- the blocking SMTP work runs off the event loop thread (the callers are async routes).
"""

import asyncio
import logging
import smtplib
import ssl
import threading
from typing import ClassVar

import pytest
from app.config import Settings
from app.services.email import (
    EmailDeliveryError,
    MockEmailTransport,
    OutboundEmail,
    SmtpEmailTransport,
    _ssl_context,
    assert_production_transport,
    build_email_change_notice,
    build_email_transport,
    build_verification_email,
    reset_email_transport,
)

SMTP_PASSWORD = "bridge-secret-hunter2"
SMTP_USER = "bridge-user@jaredstudio.com"
# 43 chars of the base64url alphabet — the shape generate_token() produces for a 32-byte token,
# which is all these tests need (the value is opaque to build_verification_email; one test only
# asserts it never reaches the log). Deliberately spelled out rather than random-looking: a
# high-entropy literal here is indistinguishable from a real leaked token to both a secret scanner
# and a human skimming the diff, and "it's only a fixture" is exactly what a real leak would claim.
TOKEN = "NOT-A-REAL-TOKEN-test-fixture-AAAAAAAAAAAAA"


def make_settings(**overrides) -> Settings:
    """Settings isolated from backend/.env and the process env, so tests are deterministic on a
    machine where real SMTP credentials exist."""
    base = dict(
        smtp_host=None,
        smtp_port=1025,
        smtp_user=SMTP_USER,
        smtp_pass=SMTP_PASSWORD,
        smtp_tls_reject_unauthorized=False,
        public_origin="https://ww.jaredstudio.com",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture(autouse=True)
def _fresh_transport_cache():
    reset_email_transport()
    yield
    reset_email_transport()


class RecordingSMTP:
    """Stands in for smtplib.SMTP: records the call order and the executing thread, sends nothing.
    Class-level `instances` lets tests inspect what the transport did."""

    instances: ClassVar[list["RecordingSMTP"]] = []
    login_exc: ClassVar[Exception | None] = None
    starttls_exc: ClassVar[Exception | None] = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent_messages = []
        self.thread_ident: int | None = None
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")
        self.context = context
        if type(self).starttls_exc is not None:
            raise type(self).starttls_exc

    def login(self, user, password):
        self.calls.append("login")
        if type(self).login_exc is not None:
            raise type(self).login_exc

    def send_message(self, mime):
        self.calls.append("send_message")
        self.thread_ident = threading.get_ident()
        self.sent_messages.append(mime)


@pytest.fixture
def recording_smtp(monkeypatch):
    RecordingSMTP.instances = []
    RecordingSMTP.login_exc = None
    RecordingSMTP.starttls_exc = None
    monkeypatch.setattr(smtplib, "SMTP", RecordingSMTP)
    return RecordingSMTP


# --- Transport selection ---------------------------------------------------------------------


def test_mock_transport_selected_when_smtp_host_unset():
    assert isinstance(build_email_transport(make_settings(smtp_host=None)), MockEmailTransport)


def test_empty_string_host_selects_mock_not_a_dial_to_nowhere():
    # docker-compose ${SMTP_HOST:-} interpolation passes "" when unset; "" must mean absent.
    assert isinstance(build_email_transport(make_settings(smtp_host="")), MockEmailTransport)


def test_smtp_transport_selected_when_host_set():
    transport = build_email_transport(make_settings(smtp_host="host.docker.internal"))
    assert isinstance(transport, SmtpEmailTransport)
    assert transport.is_mock is False


def test_mock_send_logs_body_and_never_touches_the_network(monkeypatch, caplog):
    def _explode(*args, **kwargs):
        raise AssertionError("mock transport must never construct an SMTP client")

    monkeypatch.setattr(smtplib, "SMTP", _explode)
    message = build_verification_email("op@example.com", TOKEN, settings=make_settings())
    with caplog.at_level(logging.INFO, logger="agentic.services.email"):
        asyncio.run(MockEmailTransport().send(message))
    # DELIBERATE per §5.6: the mock logs the full body (link included) — it is the only way to
    # complete the flow with no relay, and assert_production_transport is the prod guard for it.
    assert "verify-email#token=" in caplog.text
    assert "nothing sent" in caplog.text


def test_prod_guard_refuses_mock_transport():
    with pytest.raises(RuntimeError, match="refuses to boot"):
        assert_production_transport(MockEmailTransport())
    # And a real transport passes the same guard silently.
    assert_production_transport(build_email_transport(make_settings(smtp_host="relay")))


# --- Message content policy ------------------------------------------------------------------


def test_verification_link_is_absolute_and_uses_fragment_not_query():
    message = build_verification_email("op@example.com", TOKEN, settings=make_settings())
    link = f"https://ww.jaredstudio.com/verify-email#token={TOKEN}"
    assert link in message.text
    assert link in message.html
    # §5.6: the token must ride in the fragment; a `?` anywhere before it would put it in the
    # query string and therefore into proxy/edge/origin access logs.
    assert "?" not in message.text.split("#token=")[0].split("verify-email")[-1]
    assert "#token=" in link


def test_verification_email_states_ttl_and_single_use():
    message = build_verification_email("op@example.com", TOKEN, ttl_hours=24, settings=make_settings())
    for body in (message.text, message.html):
        assert "24 hours" in body
        assert "works once" in body


def test_email_change_notice_carries_nothing_redeemable():
    # §5.11: the notice goes to a possibly-attacker-controlled mailbox; no token, no link.
    message = build_email_change_notice("old-address@example.com")
    for body in (message.text, message.html):
        assert "#token=" not in body
        assert "http" not in body


def test_header_injection_rejected():
    with pytest.raises(ValueError, match="CR/LF"):
        OutboundEmail(to="op@example.com\r\nBcc: evil@x.com", subject="s", text="t", html="h")
    with pytest.raises(ValueError, match="CR/LF"):
        OutboundEmail(to="op@example.com", subject="s\nX-Injected: 1", text="t", html="h")


# --- The real send path ----------------------------------------------------------------------


def _smtp_transport(**overrides) -> SmtpEmailTransport:
    return SmtpEmailTransport(make_settings(smtp_host="host.docker.internal", **overrides))


def test_smtp_send_success_logs_no_token_and_no_local_part(recording_smtp, caplog):
    message = build_verification_email("operator-jared@example.com", TOKEN, settings=make_settings())
    with caplog.at_level(logging.DEBUG):
        asyncio.run(_smtp_transport().send(message))
    (client,) = recording_smtp.instances
    assert client.calls[-1] == "send_message"
    # §5.6: the raw token must never reach a log line on the real path, and the recipient is
    # logged as a domain only — never the local part.
    assert TOKEN not in caplog.text
    assert "operator-jared" not in caplog.text
    assert "example.com" in caplog.text


def test_smtp_starttls_precedes_auth_and_failure_never_falls_back_to_plaintext(recording_smtp):
    message = build_email_change_notice("op@example.com")
    asyncio.run(_smtp_transport().send(message))
    (ok_client,) = recording_smtp.instances
    assert ok_client.calls.index("starttls") < ok_client.calls.index("login")

    recording_smtp.instances = []
    recording_smtp.starttls_exc = smtplib.SMTPNotSupportedError("STARTTLS not offered")
    with pytest.raises(EmailDeliveryError):
        asyncio.run(_smtp_transport().send(message))
    (failed_client,) = recording_smtp.instances
    # Credentials must never be sent over a session that failed to upgrade.
    assert "login" not in failed_client.calls
    assert "send_message" not in failed_client.calls


def test_send_failure_is_generic_to_callers_and_scrubbed_in_logs(recording_smtp, caplog):
    # Pathological worst case: the raised SMTP error echoes the credentials and an internal path.
    recording_smtp.login_exc = smtplib.SMTPAuthenticationError(
        535, f"auth failed for {SMTP_USER} with password {SMTP_PASSWORD} at /app/backend/app/services/email.py".encode()
    )
    message = build_verification_email("op@example.com", TOKEN, settings=make_settings())
    with caplog.at_level(logging.DEBUG), pytest.raises(EmailDeliveryError) as excinfo:
        asyncio.run(_smtp_transport().send(message))

    # Client-visible surface (issue #13 class): constant message, severed chain, no context.
    assert str(excinfo.value) == "email delivery failed"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None or excinfo.value.__suppress_context__

    # Server-side log keeps the diagnosis but scrubs both credentials.
    assert "SMTPAuthenticationError" in caplog.text
    assert SMTP_PASSWORD not in caplog.text
    assert SMTP_USER not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_connection_failure_maps_to_delivery_error(monkeypatch):
    def _refuse(host, port, timeout=None):
        raise ConnectionRefusedError(111, f"Connection refused to {host}:{port}")

    monkeypatch.setattr(smtplib, "SMTP", _refuse)
    with pytest.raises(EmailDeliveryError) as excinfo:
        asyncio.run(_smtp_transport().send(build_email_change_notice("op@example.com")))
    assert "host.docker.internal" not in str(excinfo.value)


def test_smtp_send_runs_off_the_event_loop_thread(recording_smtp):
    async def _send_and_capture() -> int:
        await _smtp_transport().send(build_email_change_notice("op@example.com"))
        return threading.get_ident()

    loop_ident = asyncio.run(_send_and_capture())
    (client,) = recording_smtp.instances
    assert client.thread_ident is not None
    assert client.thread_ident != loop_ident, "blocking SMTP I/O must not run on the event loop"


def test_smtp_message_has_text_and_html_parts_and_verified_sender(recording_smtp):
    message = build_verification_email("op@example.com", TOKEN, settings=make_settings())
    asyncio.run(_smtp_transport().send(message))
    (mime,) = recording_smtp.instances[0].sent_messages
    assert mime["From"] == "ww.notifications@jaredstudio.com"
    assert mime["To"] == "op@example.com"
    subtypes = {part.get_content_subtype() for part in mime.iter_parts()}
    assert subtypes == {"plain", "html"}


# --- TLS posture -----------------------------------------------------------------------------


def test_tls_verification_is_on_by_default():
    context = _ssl_context(reject_unauthorized=True)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_tls_verification_optout_is_explicit_for_the_bridge_self_signed_cert(recording_smtp):
    # The opt-out exists solely for Proton Bridge's self-signed cert; the session is still TLS.
    context = _ssl_context(reject_unauthorized=False)
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False
    # And the transport actually hands its context to starttls().
    asyncio.run(_smtp_transport().send(build_email_change_notice("op@example.com")))
    (client,) = recording_smtp.instances
    assert isinstance(client.context, ssl.SSLContext)
    assert client.context.verify_mode == ssl.CERT_NONE
