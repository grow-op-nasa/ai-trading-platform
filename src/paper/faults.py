"""Test-only fault injection for the paper runtime (Sprint 18, ADR-0053).

`Faults.arm(point, exc)` makes the runtime raise `exc` the next time it passes the named point. It exists so tests
can simulate a process dying (`SimulatedCrash`, a `BaseException` so the runtime's `except Exception` cannot swallow
it), a lost broker acknowledgement (`BrokerUncertain`) or a persistence failure at an exact boundary. The CLI never
constructs or accepts a `Faults` object; the default is `NoFaults`.
"""

from __future__ import annotations

POINTS = (
    "after_intent_persisted",     # intent durable, nothing sent to the broker
    "broker_before_commit",       # broker call about to happen, nothing recorded
    "broker_after_commit",        # broker ledger committed, acknowledgement lost
    "before_portfolio_save",      # fill recorded, portfolio not yet saved
    "after_portfolio_save",       # portfolio saved, intent not yet marked APPLIED
    "before_monitoring",
)


class SimulatedCrash(BaseException):
    """Stands in for the process dying at a boundary; never caught by the runtime."""


class BrokerUncertain(Exception):
    """The paper broker call's outcome is not known to the caller (timeout / lost acknowledgement)."""


class NoFaults:
    def fire(self, point: str) -> None:  # noqa: D401
        return None


class Faults(NoFaults):
    def __init__(self) -> None:
        self._armed: dict[str, list[BaseException]] = {}

    def arm(self, point: str, exc: BaseException, times: int = 1) -> "Faults":
        if point not in POINTS:
            raise ValueError(f"unknown fault point {point!r}; valid: {POINTS}")
        self._armed.setdefault(point, []).extend([exc] * times)
        return self

    def fire(self, point: str) -> None:
        queue = self._armed.get(point)
        if queue:
            raise queue.pop(0)
