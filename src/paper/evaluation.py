"""Shared, side-effect-free decision-bar evaluation (Sprint 21, ADR-0058).

These two functions are the SINGLE implementation of "what did the registered strategy decide on the completed decision bar" and
"would the existing risk engine accept it against this portfolio". They were extracted unchanged from `PaperTradingRuntime`
so the paper runtime and the opportunity scanner cannot drift apart. Neither function writes anything, places an order, creates
an intent or touches a portfolio: `PortfolioRiskEngine.decide/decide_close` only read the portfolio they are given.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from src.signals.models import Signal, SignalDirection


def jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def signal_to_record(signal: Signal) -> dict[str, Any]:
    return {"id": str(signal.id), "timestamp": pd.Timestamp(signal.timestamp).isoformat(), "symbol": signal.symbol,
            "direction": signal.direction.value, "confidence": signal.confidence,
            "metadata": jsonable(signal.metadata)}


_SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_\-]+|(?i:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+)")


def safe_error_text(exc: BaseException, limit: int = 300) -> str:
    """Single-line error text with obvious credentials redacted; same rule the paper runtime applies to its own errors."""
    return _SECRET.sub("[redacted]", f"{type(exc).__name__}: {exc}")[:limit]


@dataclass
class SignalEval:
    outcome: str                      # SIGNAL | NO_SIGNAL | STRATEGY_ERROR | SYMBOL_MISMATCH | AMBIGUOUS
    signal: Signal | None = None
    detail: str = ""
    count: int = 0
    # Audit evidence (Sprint 21 follow-up): purely observational, never used for a decision.
    raw_count: int = 0                                  # signals the strategy returned over the WHOLE window
    last_signal: dict[str, Any] | None = None           # the most recent signal the strategy returned, whenever it was
    indicators: dict[str, dict[str, float | None]] | None = None   # columns prepare() ADDED, last two bars


def _indicator_tail(prepared: pd.DataFrame, original: pd.DataFrame) -> dict[str, dict[str, float | None]]:
    added = [c for c in prepared.columns if c not in original.columns]
    out: dict[str, dict[str, float | None]] = {}
    for ts in list(prepared.index[-2:]):
        row = {}
        for c in added:
            try:
                v = float(prepared.at[ts, c])
                row[c] = None if v != v else v
            except (TypeError, ValueError):
                row[c] = None
        out[pd.Timestamp(ts).isoformat()] = row
    return out


def evaluate_at_bar(instance, complete: pd.DataFrame, decision_bar: pd.Timestamp, symbol: str) -> SignalEval:
    """Run the strategy over COMPLETE bars only and interpret what it emitted exactly AT the decision bar."""
    try:
        prepared = instance.prepare(complete.copy())
        raw = instance.generate_signals(prepared)
        if not isinstance(raw, list) or not all(isinstance(s, Signal) for s in raw):
            raise ValueError("generate_signals() must return list[Signal]")
    except Exception as exc:  # noqa: BLE001
        return SignalEval("STRATEGY_ERROR", detail=safe_error_text(exc))
    at_bar = [s for s in raw if pd.Timestamp(s.timestamp) == decision_bar]
    try:
        extra = {"raw_count": len(raw), "last_signal": signal_to_record(raw[-1]) if raw else None,
                 "indicators": _indicator_tail(prepared, complete)}
    except Exception:  # noqa: BLE001 - evidence only; never allowed to change an outcome
        extra = {"raw_count": len(raw)}
    if any(s.symbol != symbol for s in at_bar):
        return SignalEval("SYMBOL_MISMATCH", detail="strategy emitted a signal for a different instrument", count=len(at_bar), **extra)
    if not at_bar:
        return SignalEval("NO_SIGNAL", detail="the strategy produced no signal at the decision bar", **extra)
    if len(at_bar) > 1:
        return SignalEval("AMBIGUOUS", detail=f"{len(at_bar)} signals at one decision bar; refusing to choose", count=len(at_bar), **extra)
    return SignalEval("SIGNAL", signal=at_bar[0], count=1, **extra)


@dataclass
class RiskEval:
    kind: str                         # APPROVED | REJECTED | ALREADY_FLAT | STOP_UNAVAILABLE | ENGINE_ERROR | SHORT_NOT_MODELED
    decision: Any = None
    stop_price: float | None = None
    record: dict[str, Any] | None = None     # what the runtime stores as `risk_decision`
    code: str = ""
    reason: str = ""


def assess_risk(risk, stop_policy, signal: Signal, complete: pd.DataFrame, decision_bar: pd.Timestamp, portfolio, price: float) -> RiskEval:
    stop_price: float | None = None
    try:
        if signal.direction is SignalDirection.FLAT:
            decision = risk.decide_close(signal, portfolio)
        else:
            history = complete.loc[:decision_bar]
            stop = stop_policy.stop_price(signal, history, entry_price=price)
            if not stop.available:
                return RiskEval("STOP_UNAVAILABLE", record={"approved": False, "stop_unavailable": stop.reason},
                                code="STOP_UNAVAILABLE", reason=str(stop.reason))
            stop_price = float(stop.stop_price)
            decision = risk.decide(signal, portfolio, entry_price=price, stop_price=stop_price)
    except Exception as exc:  # noqa: BLE001
        return RiskEval("ENGINE_ERROR", code="RISK_ENGINE_ERROR", reason=safe_error_text(exc))
    record = jsonable(asdict(decision))
    reason_code = getattr(decision.rejection_reason, "value", decision.rejection_reason)
    if signal.direction is SignalDirection.FLAT and not decision.approved and reason_code == "NO_POSITION_TO_CLOSE":
        return RiskEval("ALREADY_FLAT", decision, stop_price, record, "ALREADY_FLAT", "desired state FLAT is already satisfied")
    if not decision.approved:
        return RiskEval("REJECTED", decision, stop_price, record, str(reason_code), decision.explanation)
    if getattr(decision.capital_model, "value", None) == "NOT_MODELED":
        record["runtime_policy_block"] = "SHORT_EXPOSURE_NOT_MODELED"
        return RiskEval("SHORT_NOT_MODELED", decision, stop_price, record, "SHORT_NOT_MODELED",
                        "short exposure has no modelled capital/margin/borrow; the paper runtime fails closed")
    return RiskEval("APPROVED", decision, stop_price, record)


# ---------------------------------------------------------------------------------------------------- strategy resolution
class StrategyResolutionError(RuntimeError):
    """The strategy is unknown, comes from an unapproved module, or could not be constructed."""


def resolve_strategy(name: str, symbol: str, params: dict, prefixes: tuple[str, ...] = ("src.strategies",)):
    """(instance, strategy_hash) via the ordinary production registry only (development candidates can never resolve)."""
    import src.strategies  # noqa: F401  registers built-in + promoted strategies (the production path)
    from src.strategies.identity import strategy_version
    from src.strategies.registry import get_strategy_class

    try:
        cls = get_strategy_class(name)
    except KeyError as exc:
        raise StrategyResolutionError(f"strategy {name!r} is not registered in the production registry") from exc
    module = getattr(cls, "__module__", "")
    if not any(module == p or module.startswith(p + ".") for p in prefixes):
        raise StrategyResolutionError(f"strategy {name!r} comes from {module!r}, which is not an approved production strategy module")
    try:
        return cls(symbol=symbol, **params), strategy_version(cls)
    except Exception as exc:  # noqa: BLE001
        raise StrategyResolutionError(f"strategy {name!r} could not be constructed: {safe_error_text(exc)}") from exc


# ---------------------------------------------------------------------------------------------------- closed-bar preparation
@dataclass
class BarsPrep:
    kind: str                         # OK | NO_COMPLETE_BAR | CALENDAR_UNSUPPORTED | DATA_STALE | AWAITING_PROVIDER_BAR
    complete: pd.DataFrame | None = None
    dropped: list | None = None
    decision_bar: pd.Timestamp | None = None
    detail: str = ""
    expected_session: Any = None


def prepare_bars(candles: pd.DataFrame, calendar, now) -> BarsPrep:
    """Keep only COMPLETE daily bars, find the decision bar and check it is the expected latest completed session."""
    from src.paper import bars
    complete, dropped = bars.split_complete(candles, calendar, now)
    if complete.empty:
        return BarsPrep("NO_COMPLETE_BAR", detail="the dataset contains no complete daily bar", dropped=dropped)
    try:
        expected = bars.expected_completed_session(calendar, now)
    except ValueError as exc:
        return BarsPrep("CALENDAR_UNSUPPORTED", detail=safe_error_text(exc), dropped=dropped)
    decision_bar = pd.Timestamp(complete.index[-1])
    latest_session = bars.bar_session(decision_bar)
    if latest_session < expected:
        if bars.past_grace(calendar, expected, now):
            return BarsPrep("DATA_STALE", complete, dropped, decision_bar, f"latest complete bar is {latest_session}, expected {expected}", expected)
        return BarsPrep("AWAITING_PROVIDER_BAR", complete, dropped, decision_bar,
                        f"session {expected} closed but its bar is not yet published (within grace)", expected)
    return BarsPrep("OK", complete, dropped, decision_bar, expected_session=expected)
