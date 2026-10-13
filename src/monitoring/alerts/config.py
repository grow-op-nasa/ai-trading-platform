"""Validated alert configuration -- src/monitoring/alerts/config.py (ADR-0052).

Everything is OFF unless `ATP_ALERTS_ENABLED=true`. Secrets (the SMTP password) come only from
the environment and never appear in `repr()`, `describe()`, logs or reports.

Environment variables (all prefixed ATP_ALERT*):
    ATP_ALERTS_ENABLED            true|false (default false)
    ATP_ALERT_TRANSPORT           smtp | console (default smtp; console is a development transport)
    ATP_ALERT_EXISTING_FINDINGS   baseline | notify (default baseline)
    ATP_ALERT_SMTP_HOST / _PORT   (port default 587)
    ATP_ALERT_SMTP_SECURITY       starttls | ssl | none (default starttls; `none` only for loopback
                                  hosts without credentials)
    ATP_ALERT_SMTP_USERNAME / ATP_ALERT_SMTP_PASSWORD
    ATP_ALERT_FROM                sender address
    ATP_ALERT_TO                  comma-separated recipient addresses
    ATP_ALERT_SMTP_TIMEOUT_SECONDS (default 10)
    ATP_ALERT_MAX_ATTEMPTS (5) / _BACKOFF_BASE_SECONDS (60) / _BACKOFF_MAX_SECONDS (3600)
    ATP_ALERT_BATCH_LIMIT (10) / ATP_ALERT_LEASE_SECONDS (300)
    ATP_ALERT_SUBJECT_PREFIX (default ATP) / ATP_ALERT_ENVIRONMENT_LABEL
    ATP_ALERT_TEST_RECIPIENTS     comma-separated allow-list for `alerts test-send --to`
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
from dataclasses import dataclass, field
from typing import Mapping

TRANSPORT_SMTP, TRANSPORT_CONSOLE = "smtp", "console"
CHANNEL_FOR_TRANSPORT = {TRANSPORT_SMTP: "email", TRANSPORT_CONSOLE: "console"}
SECURITY_MODES = ("starttls", "ssl", "none")
EXISTING_POLICIES = ("baseline", "notify")

_ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_LOOPBACK_NAMES = {"localhost"}


class AlertConfigError(ValueError):
    """Invalid or incomplete alert configuration (CLI exit code 2)."""


def valid_address(addr: str) -> bool:
    return bool(isinstance(addr, str) and len(addr) <= 254 and _ADDRESS.match(addr) and ".." not in addr)


def is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _bool(name: str, raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    raise AlertConfigError(f"{name} must be true or false, got {raw!r}")


def _num(name: str, raw: str | None, default, cast, lo, hi):
    if raw is None or raw.strip() == "":
        return default
    try:
        v = cast(raw.strip())
    except ValueError:
        raise AlertConfigError(f"{name} must be a number, got {raw!r}") from None
    if isinstance(v, float) and not math.isfinite(v):
        raise AlertConfigError(f"{name} must be finite")
    if not lo <= v <= hi:
        raise AlertConfigError(f"{name} must be between {lo} and {hi}, got {v}")
    return v


def _clean_label(name: str, value: str) -> str:
    if any(ch in value for ch in "\r\n\0") or len(value) > 60:
        raise AlertConfigError(f"{name} must be a single line of at most 60 characters")
    return value


@dataclass(frozen=True)
class AlertConfig:
    enabled: bool = False
    transport: str = TRANSPORT_SMTP
    existing_findings: str = "baseline"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_username: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    sender: str | None = None
    recipients: tuple[str, ...] = ()
    smtp_timeout_seconds: float = 10.0
    max_attempts: int = 5
    backoff_base_seconds: float = 60.0
    backoff_max_seconds: float = 3600.0
    batch_limit: int = 10
    lease_seconds: float = 300.0
    subject_prefix: str = "ATP"
    environment_label: str = ""
    test_recipients: tuple[str, ...] = ()

    # ---------------------------------------------------------------- construction
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AlertConfig":
        e = os.environ if env is None else env
        g = e.get

        def csv(name: str) -> tuple[str, ...]:
            return tuple(p.strip() for p in (g(name) or "").split(",") if p.strip())

        cfg = cls(
            enabled=_bool("ATP_ALERTS_ENABLED", g("ATP_ALERTS_ENABLED"), False),
            transport=(g("ATP_ALERT_TRANSPORT") or TRANSPORT_SMTP).strip().lower(),
            existing_findings=(g("ATP_ALERT_EXISTING_FINDINGS") or "baseline").strip().lower(),
            smtp_host=(g("ATP_ALERT_SMTP_HOST") or "").strip() or None,
            smtp_port=_num("ATP_ALERT_SMTP_PORT", g("ATP_ALERT_SMTP_PORT"), 587, int, 1, 65535),
            smtp_security=(g("ATP_ALERT_SMTP_SECURITY") or "starttls").strip().lower(),
            smtp_username=(g("ATP_ALERT_SMTP_USERNAME") or "").strip() or None,
            smtp_password=g("ATP_ALERT_SMTP_PASSWORD") or None,
            sender=(g("ATP_ALERT_FROM") or "").strip() or None,
            recipients=csv("ATP_ALERT_TO"),
            smtp_timeout_seconds=_num("ATP_ALERT_SMTP_TIMEOUT_SECONDS", g("ATP_ALERT_SMTP_TIMEOUT_SECONDS"),
                                      10.0, float, 1.0, 120.0),
            max_attempts=_num("ATP_ALERT_MAX_ATTEMPTS", g("ATP_ALERT_MAX_ATTEMPTS"), 5, int, 1, 20),
            backoff_base_seconds=_num("ATP_ALERT_BACKOFF_BASE_SECONDS", g("ATP_ALERT_BACKOFF_BASE_SECONDS"),
                                      60.0, float, 0.0, 86400.0),
            backoff_max_seconds=_num("ATP_ALERT_BACKOFF_MAX_SECONDS", g("ATP_ALERT_BACKOFF_MAX_SECONDS"),
                                     3600.0, float, 0.0, 7 * 86400.0),
            batch_limit=_num("ATP_ALERT_BATCH_LIMIT", g("ATP_ALERT_BATCH_LIMIT"), 10, int, 1, 200),
            lease_seconds=_num("ATP_ALERT_LEASE_SECONDS", g("ATP_ALERT_LEASE_SECONDS"), 300.0, float, 5.0, 86400.0),
            subject_prefix=_clean_label("ATP_ALERT_SUBJECT_PREFIX", (g("ATP_ALERT_SUBJECT_PREFIX") or "ATP").strip()),
            environment_label=_clean_label("ATP_ALERT_ENVIRONMENT_LABEL", (g("ATP_ALERT_ENVIRONMENT_LABEL") or "").strip()),
            test_recipients=csv("ATP_ALERT_TEST_RECIPIENTS"),
        )
        return cfg

    # ---------------------------------------------------------------- validation
    def problems(self) -> list[str]:
        """All configuration problems (empty list = valid). Disabled alerting is always valid."""
        if not self.enabled:
            return []
        out: list[str] = []
        if self.transport not in CHANNEL_FOR_TRANSPORT:
            out.append(f"ATP_ALERT_TRANSPORT must be one of {sorted(CHANNEL_FOR_TRANSPORT)}, got {self.transport!r}")
        if self.existing_findings not in EXISTING_POLICIES:
            out.append(f"ATP_ALERT_EXISTING_FINDINGS must be one of {EXISTING_POLICIES}, got {self.existing_findings!r}")
        if self.backoff_base_seconds > self.backoff_max_seconds:
            out.append("backoff base must not exceed backoff max")
        if self.transport == TRANSPORT_SMTP:
            if not self.smtp_host:
                out.append("ATP_ALERT_SMTP_HOST is required for the smtp transport")
            if self.smtp_security not in SECURITY_MODES:
                out.append(f"ATP_ALERT_SMTP_SECURITY must be one of {SECURITY_MODES}, got {self.smtp_security!r}")
            if not self.sender or not valid_address(self.sender):
                out.append("ATP_ALERT_FROM must be a valid single email address")
            if not self.recipients:
                out.append("ATP_ALERT_TO must list at least one recipient")
            for r in self.recipients:
                if not valid_address(r):
                    out.append(f"invalid recipient address {r!r}")
            if bool(self.smtp_username) != bool(self.smtp_password):
                out.append("ATP_ALERT_SMTP_USERNAME and ATP_ALERT_SMTP_PASSWORD must be set together")
            if self.smtp_security == "none":
                if self.smtp_host and not is_loopback(self.smtp_host):
                    out.append("ATP_ALERT_SMTP_SECURITY=none is only allowed for loopback hosts (no plaintext to remote servers)")
                if self.smtp_username:
                    out.append("credentials may not be used with ATP_ALERT_SMTP_SECURITY=none (no plaintext credentials)")
        for r in self.test_recipients:
            if not valid_address(r):
                out.append(f"invalid ATP_ALERT_TEST_RECIPIENTS entry {r!r}")
        return out

    def validate(self) -> "AlertConfig":
        problems = self.problems()
        if problems:
            raise AlertConfigError("; ".join(problems))
        return self

    @property
    def channel(self) -> str:
        return CHANNEL_FOR_TRANSPORT.get(self.transport, self.transport)

    def event_settings(self):
        from src.monitoring.alerts.models import AlertEventSettings

        if not self.enabled:
            return AlertEventSettings(enabled=False)
        self.validate()
        return AlertEventSettings(enabled=True, notify_existing=self.existing_findings == "notify",
                                  channels=(self.channel,))

    def describe(self) -> dict:
        """Secret-free summary for CLI output and logs."""
        return {
            "enabled": self.enabled, "transport": self.transport, "channel": self.channel,
            "existing_findings_policy": self.existing_findings,
            "smtp_host": self.smtp_host, "smtp_port": self.smtp_port, "smtp_security": self.smtp_security,
            "smtp_credentials": "configured" if self.smtp_password else "none",
            "sender": self.sender, "recipient_count": len(self.recipients),
            "max_attempts": self.max_attempts, "backoff_base_seconds": self.backoff_base_seconds,
            "backoff_max_seconds": self.backoff_max_seconds, "batch_limit": self.batch_limit,
            "lease_seconds": self.lease_seconds, "subject_prefix": self.subject_prefix,
            "test_recipient_allowlist_size": len(self.test_recipients),
        }
