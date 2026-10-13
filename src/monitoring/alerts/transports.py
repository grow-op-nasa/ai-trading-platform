"""Alert transports -- src/monitoring/alerts/transports.py (ADR-0052).

A transport turns an `AlertMessage` into a `TransportResult` (delivered / retryable / permanent)
and never raises for ordinary delivery failures. Provider-specific code lives only here.

SMTP security: certificate verification is ALWAYS on (`ssl.create_default_context()`); STARTTLS is
required (no silent downgrade when the server does not offer it); plaintext is only possible on
loopback hosts without credentials (enforced in `AlertConfig`). Error text is redacted of the
username/password and bounded before it is stored or shown.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Callable, Protocol

from src.monitoring.alerts.config import TRANSPORT_CONSOLE, TRANSPORT_SMTP, AlertConfig, AlertConfigError
from src.monitoring.alerts.format import clean_line
from src.monitoring.alerts.models import AlertMessage, TransportResult


class AlertTransport(Protocol):
    name: str
    channel: str

    def validate(self) -> None: ...

    def send(self, message: AlertMessage) -> TransportResult: ...


def redact(text: str, secrets: list[str | None]) -> str:
    out = clean_line(text, 400)
    for s in secrets:
        if s and len(s) >= 3:
            out = out.replace(s, "[REDACTED]")
    return out[:240]


class ConsoleTransport:
    """Development transport: writes the rendered message to an `emit` callable (stdout by default).
    It never contacts a network; a DELIVERED status means only "printed"."""

    name, channel = TRANSPORT_CONSOLE, "console"

    def __init__(self, emit: Callable[[str], None] = print) -> None:
        self._emit = emit

    def validate(self) -> None:
        return None

    def send(self, message: AlertMessage) -> TransportResult:
        self._emit(f"--- console alert (not emailed) ---\nSubject: {message.subject}\n\n{message.body}")
        return TransportResult.ok(external_id=f"console:{message.event_id}")


SmtpFactory = Callable[[str, int, float, str, ssl.SSLContext], smtplib.SMTP]


def _default_factory(host: str, port: int, timeout: float, security: str, context: ssl.SSLContext) -> smtplib.SMTP:
    if security == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    return smtplib.SMTP(host, port, timeout=timeout)


class SmtpTransport:
    name, channel = TRANSPORT_SMTP, "email"

    def __init__(self, config: AlertConfig, *, smtp_factory: SmtpFactory | None = None,
                 ssl_context: ssl.SSLContext | None = None) -> None:
        self._cfg = config
        self._factory = smtp_factory or _default_factory
        # Verified TLS by default. A caller may inject a context ONLY for tests of the plumbing;
        # production construction never does.
        self._context = ssl_context or ssl.create_default_context()

    def validate(self) -> None:
        problems = self._cfg.problems()
        if not self._cfg.enabled:
            problems = ["alerting is not enabled (ATP_ALERTS_ENABLED)"] + problems
        if self._cfg.transport != TRANSPORT_SMTP:
            problems.append("transport is not smtp")
        if problems:
            raise AlertConfigError("; ".join(problems))

    def _build(self, message: AlertMessage) -> tuple[EmailMessage, list[str]]:
        recipients = list(message.recipients or self._cfg.recipients)
        msg = EmailMessage()
        msg["From"] = self._cfg.sender
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = message.subject
        msg["Date"] = formatdate(localtime=False, usegmt=True)
        msg["Message-ID"] = make_msgid(domain="atp-alert.local")
        msg["X-ATP-Alert-Event-ID"] = message.event_id
        if message.is_test:
            msg["X-ATP-Alert-Test"] = "true"
        msg.set_content(message.body)
        return msg, recipients

    def send(self, message: AlertMessage) -> TransportResult:
        secrets = [self._cfg.smtp_password, self._cfg.smtp_username]
        try:
            msg, recipients = self._build(message)
        except (ValueError, TypeError) as exc:
            return TransportResult.permanent(redact(f"message could not be built: {type(exc).__name__}", secrets))
        client = None
        try:
            client = self._factory(self._cfg.smtp_host, self._cfg.smtp_port, self._cfg.smtp_timeout_seconds,
                                   self._cfg.smtp_security, self._context)
            client.ehlo()
            if self._cfg.smtp_security == "starttls":
                client.starttls(context=self._context)  # no fallback to plaintext if unsupported
                client.ehlo()
            if self._cfg.smtp_username:
                client.login(self._cfg.smtp_username, self._cfg.smtp_password)
            refused = client.send_message(msg, from_addr=self._cfg.sender, to_addrs=recipients)
            if refused and len(refused) >= len(recipients):
                return TransportResult.permanent("all recipients were refused")
            detail = f"{len(refused)} recipient(s) refused" if refused else None
            return TransportResult.ok(external_id=msg["Message-ID"], detail=detail)
        except smtplib.SMTPAuthenticationError as exc:
            return TransportResult.permanent(redact(f"authentication failed ({exc.smtp_code})", secrets))
        except smtplib.SMTPNotSupportedError as exc:
            return TransportResult.permanent(redact(f"server lacks a required feature: {exc}", secrets))
        except smtplib.SMTPSenderRefused as exc:
            text = redact(f"sender refused: SMTP {exc.smtp_code}", secrets)
            return TransportResult.retryable(text) if 400 <= exc.smtp_code < 500 else TransportResult.permanent(text)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = sorted({int(v[0]) for v in exc.recipients.values()})
            text = f"recipient(s) refused by the server (SMTP {', '.join(map(str, codes))})"
            return TransportResult.retryable(text) if codes and all(400 <= c < 500 for c in codes) else TransportResult.permanent(text)
        except smtplib.SMTPResponseException as exc:
            text = redact(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}", secrets)
            return TransportResult.permanent(text) if 500 <= exc.smtp_code < 600 else TransportResult.retryable(text)
        except ssl.SSLCertVerificationError as exc:
            return TransportResult.permanent(redact(f"TLS certificate verification failed: {getattr(exc, 'verify_message', None) or exc}", secrets))
        except (smtplib.SMTPServerDisconnected, ConnectionError, socket.timeout, TimeoutError, ssl.SSLError,
                smtplib.SMTPConnectError) as exc:
            return TransportResult.retryable(redact(f"{type(exc).__name__}: {exc}", secrets))
        except OSError as exc:  # DNS failure, unreachable network, ...
            return TransportResult.retryable(redact(f"{type(exc).__name__}: {exc}", secrets))
        except smtplib.SMTPException as exc:
            return TransportResult.retryable(redact(f"{type(exc).__name__}: {exc}", secrets))
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:  # noqa: BLE001 -- closing must never mask the real result
                    pass


def build_transport(config: AlertConfig, **kwargs) -> AlertTransport:
    config.validate()
    if config.transport == TRANSPORT_CONSOLE:
        return ConsoleTransport(**{k: v for k, v in kwargs.items() if k == "emit"})
    return SmtpTransport(config, **{k: v for k, v in kwargs.items() if k in ("smtp_factory", "ssl_context")})
