"""Typed scanner records. Plain data; no behaviour that could trade."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

MAX_WATCHLIST_SYMBOLS = 50      # a finite, user-selected universe; also bounds provider requests per scan
MAX_CONCURRENCY = 8             # hard ceiling for the worker pool
DEFAULT_CONCURRENCY = 4         # conservative default for a public, unauthenticated provider
STARTER_NAME = "US Liquid Starter"
STARTER_SYMBOLS = ("QQQ", "SPY", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMD")


class ScanStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"        # every symbol was evaluated successfully
    PARTIAL = "PARTIAL"            # some symbols evaluated, some could not be
    FAILED = "FAILED"              # nothing could be evaluated (or the scan could not initialise)
    INTERRUPTED = "INTERRUPTED"    # the process ended before the scan finished; stored results are real but incomplete


class ResultStatus(str, Enum):
    SIGNAL_FOUND = "SIGNAL_FOUND"          # the registered strategy emitted a valid signal at the latest completed bar
    NO_SIGNAL = "NO_SIGNAL"                # evaluated successfully; nothing actionable
    RISK_BLOCKED = "RISK_BLOCKED"          # an authoritative read-only risk check rejected the candidate
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"  # the provider could not supply data
    NOT_EVALUABLE = "NOT_EVALUABLE"        # data arrived but could not support a valid evaluation
    ERROR = "ERROR"                        # an unexpected symbol-specific failure

    @property
    def evaluated(self) -> bool:
        return self in (ResultStatus.SIGNAL_FOUND, ResultStatus.NO_SIGNAL, ResultStatus.RISK_BLOCKED)


class RiskState(str, Enum):
    NOT_ASSESSED = "NOT_ASSESSED"    # no account/portfolio supplied or the check could not run: preflight decides
    APPROVED = "APPROVED"            # the existing risk engine accepted THIS candidate alone against the stored portfolio
    REJECTED = "REJECTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # nothing to assess (no signal / not evaluated)


@dataclass(frozen=True)
class Watchlist:
    watchlist_id: str
    name: str
    symbols: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass
class ScanResult:
    scan_id: str
    symbol: str
    status: ResultStatus
    reason_code: str
    reason: str
    decision: dict[str, Any] | None = None          # serialised strategy Signal (same record the paper runtime stores)
    decision_bar: str | None = None
    data: dict[str, Any] = field(default_factory=dict)   # provenance + freshness
    risk_state: RiskState = RiskState.NOT_APPLICABLE
    risk_detail: str = ""
    error: str | None = None
    evaluated_at: str = ""
    position: int = 0                               # watchlist order, for deterministic display


@dataclass
class ScanRecord:
    scan_id: str
    status: ScanStatus
    watchlist_id: str
    watchlist_name: str
    symbols: tuple[str, ...]
    strategy_name: str
    strategy_hash: str
    strategy_params: dict[str, Any]
    config_hash: str
    interval: str
    lookback_days: int
    account: int | None
    risk_mode: str                                  # ASSESSED | NOT_ASSESSED (+ why)
    started_at: str
    finished_at: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    request_token: str | None = None
    owner_pid: int | None = None
    heartbeat_at: str | None = None
    min_bars: int = 0
    concurrency: int = 0
    # Sprint 22: the strategy-declared requirement the scan used. Records written before it existed keep the default
    # LEGACY_HEURISTIC (their `min_bars` came from the retired parameter-name heuristic, not from the strategy).
    history_requirement: dict[str, Any] | None = None
    history_status: str = "LEGACY_HEURISTIC"
