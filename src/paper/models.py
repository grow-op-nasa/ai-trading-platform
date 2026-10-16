"""Typed records and statuses for the paper runtime (Sprint 18, ADR-0053)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class CycleStatus(str, Enum):
    """Outcome of one decision cycle. A no-op never looks like a failed order, and a blocked cycle is never
    reported as executed."""

    STARTED = "STARTED"                            # transient; a cycle left here crashed mid-flight
    NO_NEW_BAR = "NO_NEW_BAR"                      # completed: no new complete decision bar
    NO_ACTION = "NO_ACTION"                        # completed: no actionable decision for the bar
    RISK_REJECTED = "RISK_REJECTED"                # completed: risk (or runtime policy) refused; no order
    ORDER_REJECTED = "ORDER_REJECTED"              # completed: paper broker / cost model refused; no fill
    ORDER_FILLED = "ORDER_FILLED"                  # completed: filled and applied to the portfolio
    AWAITING_EXECUTION = "AWAITING_EXECUTION"      # intent exists but its outcome is not yet conclusive
    BLOCKED = "BLOCKED"                            # data or safety preconditions prevented a decision
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    FAILED = "FAILED"                              # infrastructure / persistence / strategy error


NON_TERMINAL_CYCLE = (CycleStatus.STARTED, CycleStatus.AWAITING_EXECUTION)


class IntentState(str, Enum):
    INTENT_RECORDED = "INTENT_RECORDED"            # durable, nothing has reached the paper broker ledger
    FILLED_AT_BROKER = "FILLED_AT_BROKER"          # broker ledger holds the fill; portfolio not yet confirmed
    APPLIED = "APPLIED"                            # portfolio reflects the fill (terminal)
    REJECTED = "REJECTED"                          # terminal: broker/cost model refused, nothing executed


UNRESOLVED_INTENT = (IntentState.INTENT_RECORDED, IntentState.FILLED_AT_BROKER)


class ReconStatus(str, Enum):
    MATCH = "MATCH"
    PENDING = "PENDING"        # unresolved intents exist; values may legitimately lag
    MISMATCH = "MISMATCH"


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_EVALUABLE = "NOT_EVALUABLE"


@dataclass
class PreflightCheck:
    name: str
    status: CheckStatus
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status.value, "detail": self.detail}


@dataclass
class PreflightReport:
    checks: list[PreflightCheck] = field(default_factory=list)

    @property
    def overall(self) -> CheckStatus:
        order = (CheckStatus.FAIL, CheckStatus.BLOCKED, CheckStatus.NOT_EVALUABLE)
        present = {c.status for c in self.checks}
        for s in order:
            if s in present:
                return s
        return CheckStatus.PASS

    @property
    def ready(self) -> bool:
        return self.overall is CheckStatus.PASS

    def to_dict(self) -> dict[str, Any]:
        return {"overall": self.overall.value, "ready": self.ready, "checks": [c.to_dict() for c in self.checks]}


@dataclass
class CycleRecord:
    cycle_id: str
    experiment_id: int
    status: CycleStatus
    started_at: str
    idempotency_key: str | None = None
    finished_at: str | None = None
    reason_code: str | None = None
    reason: str | None = None
    strategy_name: str | None = None
    strategy_hash: str | None = None
    config_hash: str | None = None
    symbol: str | None = None
    interval: str | None = None
    decision_bar: str | None = None
    dataset: dict[str, Any] | None = None
    signal: dict[str, Any] | None = None
    risk_decision: dict[str, Any] | None = None
    intent_id: str | None = None
    client_order_id: str | None = None
    fill: dict[str, Any] | None = None
    reconciliation_id: int | None = None
    reconciliation_status: str | None = None
    monitoring: dict[str, Any] | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CycleRecord":
        d = dict(d)
        d["status"] = CycleStatus(d["status"])
        return cls(**d)


@dataclass
class IntentRecord:
    intent_id: str
    cycle_id: str
    experiment_id: int
    client_order_id: str
    symbol: str
    decision_bar: str
    state: IntentState
    side: str
    quantity: float
    approved_quantity: float
    direction: str
    reference_price: float
    stop_price: float | None
    signal: dict[str, Any]
    risk_decision: dict[str, Any]
    execution_config: dict[str, Any]
    created_at: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IntentRecord":
        d = dict(d)
        d["state"] = IntentState(d["state"])
        return cls(**d)


@dataclass
class FillRecord:
    client_order_id: str
    symbol: str
    side: str
    direction: str
    quantity: float
    fill_price: float
    reference_price: float | None
    fee: float
    slippage_amount: float
    cash_delta: float
    signal_id: str
    order_timestamp: str
    fill_timestamp: str | None
    seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FillRecord":
        return cls(**d)


@dataclass
class ReconRecord:
    experiment_id: int
    status: ReconStatus
    created_at: str
    cycle_id: str | None = None
    discrepancies: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    recon_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d
