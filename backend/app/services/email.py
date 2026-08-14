"""Transactional email for the auth system: verification links and security notices.

Two transports behind one interface, selected by config (AUTH_THREAT_MODEL.md §5.6):

- ``SmtpEmailTransport`` — stdlib ``smtplib`` over STARTTLS, configured entirely from env
  (``SMTP_HOST/PORT/USER/PASS/FROM``, ``SMTP_TLS_REJECT_UNAUTHORIZED``). In this deployment the env
  points at Proton Mail Bridge on the docker host, but nothing here knows that — any relay works.
  The blocking socket work runs on a worker thread via ``asyncio.to_thread`` so an async FastAPI
  route is never pinned on SMTP.
- ``MockEmailTransport`` — selected automatically when ``SMTP_HOST`` is unset. Logs the message and
  sends nothing, so dev and CI (which have no Bridge) never open a socket. This mirrors 9b's
  deliberate design.

Threat model (AUTH_THREAT_MODEL.md §5.6, §5.11 — read them before changing this file):

- **Token leakage via URLs**: the verification link carries the token in the URL *fragment*
  (``…/verify-email#token=…``), never the query string. Fragments are not transmitted on the wire,
  so no proxy, edge, or origin access log can contain the token.
- **Token leakage via our own logs**: the SMTP transport logs only ``{recipient domain, subject}``
  — never the body, never the local part of the address, never the token. The mock transport DOES
  log the full body, link included: that is its purpose (the only way to complete the flow with no
  relay configured), it is only ever selected when ``SMTP_HOST`` is unset, and §5.6 specifies a
  startup guard refusing the mock under the prod profile — ``assert_production_transport`` below is
  that guard, for the startup owner to wire.
- **Credential leakage on failure** (the issue #13 class): a send failure raises
  ``EmailDeliveryError`` whose message is a constant, with the exception chain severed
  (``from None``) so no SMTP detail can ride into a client-visible surface. Full detail is logged
  server-side only, with the SMTP username/password scrubbed from the text first — belt over the
  global ``SecretRedactionFilter``'s suspenders.
- **SMTP header injection**: recipients and subjects at the call sites are server-derived (DB
  address, literal subject), and ``OutboundEmail`` additionally rejects any CR/LF in header-bound
  values, so a newline can never mint an extra header.
- **TLS downgrade**: STARTTLS is mandatory — there is no plaintext fallback, and AUTH happens only
  after the upgrade, so credentials never cross the wire unencrypted. Certificate verification is
  ON by default; ``SMTP_TLS_REJECT_UNAUTHORIZED=false`` exists solely for Proton Bridge's
  self-signed certificate on the loopback/host-gateway hop and is the operator's explicit,
  documented opt-out (the session is still encrypted — only chain validation is skipped).
"""

from __future__ import annotations

import asyncio
import html as html_mod
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from typing import Protocol
from urllib.parse import quote

from app.config import Settings, get_settings

logger = logging.getLogger("agentic.services.email")

# Header-bound values must never contain CR/LF (SMTP header injection, §5.6).
_CRLF = re.compile(r"[\r\n]")

# Subjects are module-level constants on purpose: §5.6's header-injection defense is that no
# request-controlled value ever reaches a header, and a literal here makes that auditable.
VERIFICATION_SUBJECT = "Verify your email — Agentic Robinhood dashboard"
EMAIL_CHANGE_SUBJECT = "Security notice: account email changed — Agentic Robinhood dashboard"


class EmailDeliveryError(RuntimeError):
    """A send failed. The message is a constant so nothing transport-specific (host, credentials,
    file paths) can leak into any client-visible surface — the issue #13 defect class. The real
    cause is logged server-side (scrubbed) before this is raised, and the chain is severed."""

    def __init__(self) -> None:
        super().__init__("email delivery failed")


@dataclass(frozen=True)
class OutboundEmail:
    """One message to deliver. Plain text always; HTML alongside it."""

    to: str
    subject: str
    text: str
    html: str

    def __post_init__(self) -> None:
        # Defense-in-depth against header injection: call sites are server-derived already, but a
        # CR/LF in a header value must fail loudly here rather than mint a Bcc: downstream.
        for field_name, value in (("to", self.to), ("subject", self.subject)):
            if _CRLF.search(value):
                raise ValueError(f"CR/LF in email {field_name} header value")
        if "@" not in self.to:
            raise ValueError("recipient is not an email address")

    @property
    def to_domain(self) -> str:
        """The only recipient component that may appear in a log line (no local parts)."""
        return self.to.rsplit("@", 1)[1]


class EmailTransport(Protocol):
    """Deliver one message. Raises EmailDeliveryError on failure — callers decide whether a
    failure is fatal (for verification resends it never is; suppression stays invisible, §5.6)."""

    is_mock: bool

    async def send(self, message: OutboundEmail) -> None: ...


class MockEmailTransport:
    """Log-only transport: dev fallback and the default under test. Sends nothing.

    DELIBERATE (§5.6): this logs the full text body — it contains the verification URL, which is
    the only way to complete the flow with no relay configured. It is selected only when SMTP_HOST
    is unset, and ``assert_production_transport`` exists so the prod profile refuses to boot with
    it. Do not "fix" the body logging; the guard is the defense, not redaction here.
    """

    is_mock = True

    async def send(self, message: OutboundEmail) -> None:
        logger.info(
            "mock email transport (SMTP_HOST unset — nothing sent): to=%s subject=%r body:\n%s",
            message.to,
            message.subject,
            message.text,
        )


class SmtpEmailTransport:
    """Real SMTP over STARTTLS via stdlib smtplib, run on a worker thread per send."""

    is_mock = False

    def __init__(self, settings: Settings) -> None:
        if not settings.smtp_host:
            raise ValueError("SmtpEmailTransport requires SMTP_HOST; use build_email_transport()")
        self._host: str = settings.smtp_host
        self._port = settings.smtp_port
        self._user = settings.smtp_user
        self._password = settings.smtp_pass
        self._from = settings.smtp_from
        self._timeout = settings.smtp_timeout_seconds
        self._ssl_context = _ssl_context(settings.smtp_tls_reject_unauthorized)

    async def send(self, message: OutboundEmail) -> None:
        # The callers are async FastAPI routes: the blocking socket work must never run on the
        # event loop. to_thread uses the loop's default executor, which is bounded, and every
        # socket operation carries smtp_timeout_seconds, so a dead relay fails fast instead of
        # accumulating pinned threads.
        try:
            await asyncio.to_thread(self._send_sync, message)
        except (smtplib.SMTPException, OSError) as exc:
            # OSError covers refused/unreachable/timeout sockets and ssl.SSLError. Log the real
            # cause server-side only, with credentials scrubbed from the text (an SMTP server's
            # error string can echo the AUTH exchange), then raise the constant-message error with
            # the chain severed so nothing transport-specific reaches a client surface (issue #13).
            logger.error(
                "smtp send to domain %s failed: %s: %s",
                message.to_domain,
                type(exc).__name__,
                self._scrub(str(exc)),
            )
            raise EmailDeliveryError() from None
        logger.info("email sent: subject=%r to domain %s", message.subject, message.to_domain)

    def _send_sync(self, message: OutboundEmail) -> None:
        mime = MimeMessage()
        mime["From"] = self._from
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")

        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as client:
            client.ehlo()
            # STARTTLS is mandatory — no plaintext fallback. If the relay cannot upgrade, this
            # raises before login() is ever reached, so credentials never cross the wire in clear.
            client.starttls(context=self._ssl_context)
            client.ehlo()
            if self._user is not None and self._password is not None:
                client.login(self._user, self._password)
            client.send_message(mime)

    def _scrub(self, detail: str) -> str:
        """Remove the SMTP credentials from server-side log text. The global redaction filter is
        the backstop; this makes the guarantee local and testable."""
        for secret in (self._password, self._user):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        return detail


def _ssl_context(reject_unauthorized: bool) -> ssl.SSLContext:
    """TLS context for the STARTTLS upgrade.

    Default: full verification (system trust store + hostname check). The False branch exists for
    exactly one documented case — Proton Mail Bridge presents a self-signed certificate on the
    loopback/host-gateway hop, so chain validation cannot succeed there (9b carries the same
    SMTP_TLS_REJECT_UNAUTHORIZED opt-out for the same reason). The session is still TLS-encrypted;
    only certificate validation is skipped, and only on the operator's explicit env opt-in — never
    silently.
    """
    context = ssl.create_default_context()
    if not reject_unauthorized:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def build_email_transport(settings: Settings | None = None) -> EmailTransport:
    """SMTP when SMTP_HOST is configured, the log-only mock otherwise (§5.6). Never raises for an
    unset host — CI and dev must run without a Bridge."""
    settings = settings or get_settings()
    if settings.smtp_host:
        return SmtpEmailTransport(settings)
    return MockEmailTransport()


_transport: EmailTransport | None = None


def get_email_transport() -> EmailTransport:
    """Process-wide transport, built lazily from settings on first use."""
    global _transport
    if _transport is None:
        _transport = build_email_transport()
    return _transport


def reset_email_transport() -> None:
    """Drop the cached transport (tests, or after a settings reload)."""
    global _transport
    _transport = None


def assert_production_transport(transport: EmailTransport | None = None) -> None:
    """§5.6 startup guard: a prod-profile boot must refuse the mock transport, because the mock
    logs full bodies (verification link included) by design. Call this from app startup wiring
    when the profile is prod; it is a function here (not a main.py hook) because main.py is owned
    by the startup change, not this module."""
    transport = transport or get_email_transport()
    if transport.is_mock:
        raise RuntimeError(
            "SMTP_HOST is unset, so the log-only mock email transport would be used. "
            "The production profile refuses to boot this way (AUTH_THREAT_MODEL.md §5.6): "
            "the mock logs full message bodies, verification links included. Configure SMTP_*."
        )


# --------------------------------------------------------------------------------------------
# Message builders. Everything below is content policy: what may and may not appear in a message
# (§5.6, §5.11). Recipients come from the DB and subjects are the literal constants above.
# --------------------------------------------------------------------------------------------


def build_verification_link(token: str, settings: Settings | None = None) -> str:
    """Absolute verification URL against the configured public origin, with the token in the URL
    FRAGMENT — never the query string. Fragments are not transmitted on the wire, so no proxy,
    edge, or origin access log can contain the token (§5.6; 9b fix-pass SF-2, ported)."""
    settings = settings or get_settings()
    origin = settings.public_origin.rstrip("/")
    return f"{origin}/verify-email#token={quote(token, safe='')}"


def build_verification_email(
    to: str,
    token: str,
    *,
    ttl_hours: int = 24,
    settings: Settings | None = None,
) -> OutboundEmail:
    """The email-verification message (§5.6).

    Content policy: the token appears ONLY inside the fragment link. The message never carries
    account numbers, holdings, or anything session-granting — redeeming the link stamps
    ``email_verified_at`` and nothing else, and the body says so, so a phished copy of this
    message teaches an attacker nothing that yields a session.
    """
    link = build_verification_link(token, settings)
    text = (
        "Confirm this email address for the Agentic Robinhood dashboard.\n"
        "\n"
        f"Open this link to verify:\n{link}\n"
        "\n"
        f"The link expires in {ttl_hours} hours, works once, and only confirms the address —\n"
        "it does not sign anyone in. If you did not request this, you can ignore this message;\n"
        "nothing changes until the link is opened.\n"
    )
    href = html_mod.escape(link, quote=True)
    html = (
        "<p>Confirm this email address for the <strong>Agentic Robinhood dashboard</strong>.</p>"
        f'<p><a href="{href}">Verify this email address</a></p>'
        f"<p>Or open this link:<br><code>{html_mod.escape(link)}</code></p>"
        f"<p>The link expires in {ttl_hours} hours, works once, and only confirms the address — "
        "it does not sign anyone in. If you did not request this, you can ignore this message; "
        "nothing changes until the link is opened.</p>"
    )
    return OutboundEmail(to=to, subject=VERIFICATION_SUBJECT, text=text, html=html)


def build_email_change_notice(to: str) -> OutboundEmail:
    """The email-change security notice (§5.11), sent to BOTH the old and the new address so an
    unauthorized change is visible at the mailbox the real operator still controls.

    Content policy: NO token and NO link — this message must remain useful even if the mailbox is
    the attacker's, so it carries nothing redeemable. It also names neither address (each recipient
    knows which mailbox received it), keeping the other operator's address out of a possibly-
    compromised inbox.
    """
    text = (
        "The email address on your Agentic Robinhood dashboard operator account was just changed.\n"
        "\n"
        "If you made this change, no action is needed — a separate verification message was sent\n"
        "to the new address.\n"
        "\n"
        "If you did NOT make this change, treat your password and authenticator as compromised:\n"
        "sign in from a trusted machine to revoke all sessions, or use the operator CLI on the\n"
        "host. Email is not an account-recovery channel for this system, so this change alone\n"
        "does not grant anyone access — but act on it.\n"
    )
    html = (
        "<p>The email address on your <strong>Agentic Robinhood dashboard</strong> operator "
        "account was just changed.</p>"
        "<p>If you made this change, no action is needed — a separate verification message was "
        "sent to the new address.</p>"
        "<p>If you did <strong>not</strong> make this change, treat your password and "
        "authenticator as compromised: sign in from a trusted machine to revoke all sessions, or "
        "use the operator CLI on the host. Email is not an account-recovery channel for this "
        "system, so this change alone does not grant anyone access — but act on it.</p>"
    )
    return OutboundEmail(to=to, subject=EMAIL_CHANGE_SUBJECT, text=text, html=html)


# --------------------------------------------------------------------------------------------
# High-level API for the auth routers.
# --------------------------------------------------------------------------------------------


async def send_verification_email(to: str, token: str, *, ttl_hours: int = 24) -> None:
    """Build and send the verification message via the process transport. Raises
    EmailDeliveryError on transport failure; the caller decides whether that is fatal."""
    await get_email_transport().send(build_verification_email(to, token, ttl_hours=ttl_hours))


async def send_email_change_notice(to: str) -> None:
    """Build and send the email-change notice via the process transport."""
    await get_email_transport().send(build_email_change_notice(to))
