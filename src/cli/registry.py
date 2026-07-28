"""Registry for `atp doctor` health checks.

Same pattern as `src/indicators/registry.py`: a health check is a
plain, no-argument function that returns a `CheckResult`, registered
with `@register_check("Display Name")`. Adding a new check (e.g. a
Broker Connection check once `src/broker/` exists) means writing one
function and decorating it -- `src/cli/doctor.py` never needs to
change. This is the extensibility principle from `DECISIONS.md`,
ADR-0000, applied to the doctor command itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class CheckStatus(Enum):
    """Outcome of a single health check."""

    OK = "OK"
    FAIL = "FAIL"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


@dataclass
class CheckResult:
    """The outcome of one named health check.

    Args:
        name: display name, e.g. "Market Data". Must match the name
            passed to `@register_check`.
        status: one of `CheckStatus`.
        detail: short human-readable context -- an error message on
            FAIL, or a reason on NOT_IMPLEMENTED. Not shown for OK
            unless present, to keep a healthy run's output uncluttered.
    """

    name: str
    status: CheckStatus
    detail: str = field(default="")


CheckFunc = Callable[[], CheckResult]

_CHECKS: dict[str, CheckFunc] = {}


class DuplicateCheckError(Exception):
    """Raised when two checks are registered under the same name."""


def register_check(name: str) -> Callable[[CheckFunc], CheckFunc]:
    """Decorator that registers a health check under `name`.

    Raises:
        DuplicateCheckError: `name` is already registered.
    """

    def decorator(func: CheckFunc) -> CheckFunc:
        if name in _CHECKS:
            raise DuplicateCheckError(f"Check '{name}' is already registered")
        _CHECKS[name] = func
        return func

    return decorator


def registered_checks() -> dict[str, CheckFunc]:
    """All registered checks, in registration order."""
    return dict(_CHECKS)


def _reset_registry_for_tests() -> None:
    """Test-only helper: clear the registry so tests can register fakes
    without colliding with the real checks imported elsewhere."""
    _CHECKS.clear()
