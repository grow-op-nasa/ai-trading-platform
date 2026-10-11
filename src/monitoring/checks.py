"""Deterministic monitors -- src/monitoring/checks.py (ADR-0050).

Seven independent checks. Each takes a `PlatformState` + `MonitoringConfig`
and returns a `CheckResult`:

    market_data_freshness       session-aware: no alerts for closed markets
    market_data_integrity       provider/validation outcome via MarketDataService
    portfolio_valuation         PortfolioValuationService, read-only
    risk_exposure               Portfolio/RiskLimits (no re-derived formulas)
    portfolio_drawdown          analytics.metrics.drawdown_curve on real history
    execution_consistency       provable order/fill/trade/reconciliation anomalies
    strategy_model_compat       StrategyRegistry / model provenance

A check that lacks evidence returns NOT_EVALUABLE with a reason. It never
guesses, never reports healthy on missing data, and never imports an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol

import pandas as pd

from src.analytics.metrics import drawdown_curve
from src.analytics.valuation import PortfolioValuationService
from src.calendar.base import TradingCalendar
from src.data.base import Interval
from src.monitoring.config import MonitoringConfig
from src.monitoring.models import (
    CheckResult,
    CheckStatus,
    FindingCandidate,
    FindingSeverity,
    MonitoringEvidence,
)
from src.monitoring.state import MarketDataObservation, PlatformState
from src.portfolio.position import PositionSide
from src.strategies.registry import get_strategy_class

W, C = FindingSeverity.WARNING, FindingSeverity.CRITICAL

_INTRADAY_MINUTES = {
    Interval.MINUTE_1: 1,
    Interval.MINUTE_5: 5,
    Interval.MINUTE_15: 15,
    Interval.MINUTE_30: 30,
    Interval.HOUR_1: 60,
}


@dataclass
class CheckContext:
    now: datetime
    calendar: TradingCalendar


class Check(Protocol):
    check_id: str

    def run(self, state: PlatformState, config: MonitoringConfig, ctx: CheckContext) -> CheckResult: ...


def _ev(reference: str, kind: str, summary: str, **data) -> MonitoringEvidence:
    return MonitoringEvidence(reference=reference, kind=kind, summary=summary, data=data)


# ---------------------------------------------------------------------------
# Freshness (session-aware)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Freshness:
    status: CheckStatus
    detail: str
    data: dict


def evaluate_freshness(
    obs: MarketDataObservation, cfg: MonitoringConfig, ctx: CheckContext
) -> Freshness:
    """Is the latest bar as fresh as the market calendar says it should be?

    Daily bars: count *expected trading sessions* with no bar. Intraday
    bars: age of the latest bar measured from the later of "now" (while
    the regular session is open) or the most recent session close
    (market closed) -- so nights, weekends and holidays never look stale.
    """
    if not obs.ok or obs.dataset_end is None:
        return Freshness(CheckStatus.NOT_EVALUABLE, "no usable dataset to evaluate", {})
    cal, now = ctx.calendar, pd.Timestamp(ctx.now).tz_convert("UTC")
    last = pd.Timestamp(obs.dataset_end)
    if last.tzinfo is None:
        return Freshness(CheckStatus.NOT_EVALUABLE, "latest bar timestamp is timezone-naive", {})
    try:
        local_today = now.tz_convert(cal.local_timezone).date()
        if obs.interval == Interval.DAY_1:
            return _daily(obs, cfg, cal, now, local_today, last)
        if obs.interval in _INTRADAY_MINUTES:
            return _intraday(obs, cfg, cal, now, local_today, last)
    except ValueError as exc:  # e.g. year outside the calendar's verified range
        return Freshness(CheckStatus.NOT_EVALUABLE, f"calendar cannot evaluate: {exc}", {})
    return Freshness(
        CheckStatus.NOT_EVALUABLE, f"freshness is not defined for {obs.interval.value} bars", {}
    )


def _previous_session(cal: TradingCalendar, day) -> object:
    d = day - timedelta(days=1)
    for _ in range(14):
        if cal.is_session(d):
            return d
        d -= timedelta(days=1)
    raise ValueError("no trading session found within 14 days")


def _daily(obs, cfg, cal, now, local_today, last) -> Freshness:
    grace = timedelta(minutes=cfg.daily_close_grace_minutes)
    if cal.is_session(local_today) and now >= cal.session_close(local_today) + grace:
        expected = local_today
    else:
        expected = _previous_session(cal, local_today)
    # Daily bars are stamped at UTC midnight of their trading date (canonical
    # form, ADR-0041), so the bar's *UTC date* is its session -- converting to
    # exchange-local time would shift it back a day.
    latest = last.tz_convert("UTC").date()
    missing = len([d for d in cal.sessions_between(latest + timedelta(days=1), expected)]) if latest < expected else 0
    data = {
        "latest_bar_session": latest.isoformat(),
        "expected_session": expected.isoformat(),
        "missing_sessions": missing,
        "warning_sessions": cfg.daily_stale_warning_sessions,
        "critical_sessions": cfg.daily_stale_critical_sessions,
    }
    if missing >= cfg.daily_stale_critical_sessions:
        return Freshness(CheckStatus.CRITICAL, f"{missing} expected session(s) missing", data)
    if missing >= cfg.daily_stale_warning_sessions:
        return Freshness(CheckStatus.WARNING, f"{missing} expected session(s) missing", data)
    return Freshness(CheckStatus.HEALTHY, "latest daily bar is current for the market calendar", data)


def _intraday(obs, cfg, cal, now, local_today, last) -> Freshness:
    minutes = _INTRADAY_MINUTES[obs.interval]
    market_open = False
    if cal.is_session(local_today):
        if cal.session_open(local_today) <= now < cal.session_close(local_today):
            market_open = True
            reference = now
        elif now >= cal.session_close(local_today):
            reference = cal.session_close(local_today)
        else:
            reference = cal.session_close(_previous_session(cal, local_today))
    else:
        reference = cal.session_close(_previous_session(cal, local_today))
    age_bars = max((reference - last).total_seconds(), 0) / (minutes * 60)
    data = {
        "latest_bar": last.isoformat(),
        "reference_time": reference.isoformat(),
        "market_open": market_open,
        "age_in_bars": round(age_bars, 2),
        "warning_bars": cfg.intraday_stale_warning_bars,
        "critical_bars": cfg.intraday_stale_critical_bars,
    }
    if age_bars >= cfg.intraday_stale_critical_bars:
        return Freshness(CheckStatus.CRITICAL, f"latest bar is {age_bars:.1f} bars old", data)
    if age_bars >= cfg.intraday_stale_warning_bars:
        return Freshness(CheckStatus.WARNING, f"latest bar is {age_bars:.1f} bars old", data)
    why = "market open and bar current" if market_open else "market closed; data current through last close"
    return Freshness(CheckStatus.HEALTHY, why, data)


class MarketDataFreshnessCheck:
    check_id = "market_data_freshness"
    RULE = "market_data.stale"

    def run(self, state, config, ctx) -> CheckResult:
        if state.market_data_unavailable_reason:
            return CheckResult.not_evaluable(self.check_id, state.market_data_unavailable_reason)
        if not state.market_data_targets:
            return CheckResult.not_evaluable(self.check_id, "no market-data targets (no paper state or watchlist)")
        result = CheckResult(self.check_id, CheckStatus.HEALTHY)
        evaluated = 0
        for target in state.market_data_targets:
            obs = state.market_data.get(target.key)
            if obs is None:
                result.notes.append(f"{target.key}: not observed")
                continue
            fr = evaluate_freshness(obs, config, CheckContext(ctx.now, ctx.calendar))
            if fr.status == CheckStatus.NOT_EVALUABLE:
                result.notes.append(f"{target.key}: not evaluable ({fr.detail})")
                continue
            evaluated += 1
            result.mark_checked(self.RULE, symbol=target.symbol, interval=target.interval.value)
            if fr.status in (CheckStatus.WARNING, CheckStatus.CRITICAL):
                result.candidates.append(
                    FindingCandidate(
                        rule_id=self.RULE,
                        category="market_data",
                        severity=C if fr.status == CheckStatus.CRITICAL else W,
                        title=f"Stale market data for {target.symbol} ({target.interval.value})",
                        description=f"{fr.detail}. The market calendar says a fresher bar is expected.",
                        observed_value=fr.data,
                        expected_value="latest bar within the configured freshness threshold",
                        entities={"symbol": target.symbol, "interval": target.interval.value},
                        evidence=[_ev(f"market_data:{target.key}", "freshness", fr.detail, **fr.data)],
                    )
                )
        if evaluated == 0:
            return CheckResult.not_evaluable(
                self.check_id, "no target could be evaluated: " + "; ".join(result.notes or ["no observations"])
            )
        result.finalize_status()
        result.summary = f"{evaluated} target(s) evaluated"
        return result


# ---------------------------------------------------------------------------
# Integrity (reuses MarketDataService's validation outcome)
# ---------------------------------------------------------------------------


class MarketDataIntegrityCheck:
    check_id = "market_data_integrity"

    def run(self, state, config, ctx) -> CheckResult:
        if state.market_data_unavailable_reason:
            return CheckResult.not_evaluable(self.check_id, state.market_data_unavailable_reason)
        if not state.market_data_targets:
            return CheckResult.not_evaluable(self.check_id, "no market-data targets (no paper state or watchlist)")
        result = CheckResult(self.check_id, CheckStatus.HEALTHY)
        seen = 0
        for target in state.market_data_targets:
            obs = state.market_data.get(target.key)
            if obs is None:
                continue
            seen += 1
            ent = {"symbol": target.symbol, "interval": target.interval.value}
            ref = f"market_data:{target.key}"
            for rule in ("market_data.retrieval_failed", "market_data.insufficient_data",
                         "market_data.coverage_gaps", "market_data.identity_unavailable"):
                result.mark_checked(rule, **ent)
            if not obs.ok:
                result.candidates.append(
                    FindingCandidate(
                        "market_data.retrieval_failed", "market_data", C,
                        f"Market data unavailable for {target.symbol} ({target.interval.value})",
                        f"MarketDataService could not return a validated dataset: {obs.error_code}.",
                        observed_value={"error_code": obs.error_code, "message": obs.error_message},
                        expected_value="a validated canonical dataset",
                        entities=ent,
                        evidence=[_ev(ref, "provider_outcome", str(obs.error_code), error_code=obs.error_code,
                                      message=obs.error_message)],
                    )
                )
                continue
            if obs.rows < config.min_bars:
                result.candidates.append(
                    FindingCandidate(
                        "market_data.insufficient_data", "market_data", W,
                        f"Insufficient data for {target.symbol}",
                        f"Only {obs.rows} bar(s) returned; at least {config.min_bars} expected.",
                        observed_value=obs.rows, expected_value=f">= {config.min_bars}", entities=ent,
                        evidence=[_ev(ref, "dataset", "row count", rows=obs.rows)],
                    )
                )
            if obs.gap_count:
                result.candidates.append(
                    FindingCandidate(
                        "market_data.coverage_gaps", "market_data", W,
                        f"{obs.gap_count} in-session gap(s) in {target.symbol} data",
                        "The validation report lists timestamps missing inside open sessions.",
                        observed_value=obs.gap_count, expected_value=0, entities=ent,
                        evidence=[_ev(ref, "validation_report", "gaps", gap_count=obs.gap_count,
                                      suspicious_count=obs.suspicious_count)],
                    )
                )
            if not obs.content_hash:
                result.candidates.append(
                    FindingCandidate(
                        "market_data.identity_unavailable", "market_data", W,
                        f"No dataset identity for {target.symbol}",
                        "The canonical dataset carried no content hash.",
                        observed_value=None, expected_value="a content hash", entities=ent,
                        evidence=[_ev(ref, "dataset", "content hash missing")],
                    )
                )
        if seen == 0:
            return CheckResult.not_evaluable(self.check_id, "no market data was observed")
        result.finalize_status()
        result.summary = f"{seen} target(s) inspected"
        return result


# ---------------------------------------------------------------------------
# Valuation (read-only)
# ---------------------------------------------------------------------------


class PortfolioValuationCheck:
    check_id = "portfolio_valuation"

    def __init__(self, service: PortfolioValuationService | None = None) -> None:
        self._service = service or PortfolioValuationService()

    def run(self, state, config, ctx) -> CheckResult:
        if state.portfolio is None:
            return CheckResult.not_evaluable(self.check_id, "no paper portfolio available")
        portfolio = state.portfolio
        if portfolio.position_count == 0:
            r = CheckResult(self.check_id, CheckStatus.HEALTHY, summary="no open positions to value")
            return r
        if state.market_data_unavailable_reason and not state.market_data:
            return CheckResult.not_evaluable(self.check_id, state.market_data_unavailable_reason)

        def lookup(symbol: str):
            for obs in state.market_data.values():
                if obs.symbol == symbol and obs.ok:
                    return obs.last_close
            return None

        snapshot = self._service.value(portfolio, lookup)
        result = CheckResult(self.check_id, CheckStatus.HEALTHY)
        for pv in snapshot.positions:
            ent = {"symbol": pv.symbol}
            result.mark_checked("valuation.missing_price", **ent)
            result.mark_checked("valuation.stale_input", **ent)
            if not pv.price_available:
                result.candidates.append(
                    FindingCandidate(
                        "valuation.missing_price", "valuation", W,
                        f"No valuation price for {pv.symbol}",
                        "The open position could not be marked to market; its value is undefined, not zero.",
                        observed_value=None, expected_value="a current market price", entities=ent,
                        evidence=[_ev(f"portfolio:{state.experiment_id}", "valuation", pv.warning or "no price",
                                      symbol=pv.symbol, quantity=pv.quantity)],
                    )
                )
                continue
            for obs in state.market_data.values():
                if obs.symbol == pv.symbol and obs.ok:
                    fr = evaluate_freshness(obs, config, CheckContext(ctx.now, ctx.calendar))
                    if fr.status in (CheckStatus.WARNING, CheckStatus.CRITICAL):
                        result.candidates.append(
                            FindingCandidate(
                                "valuation.stale_input", "valuation", W,
                                f"Stale valuation input for {pv.symbol}",
                                f"The price used to value {pv.symbol} comes from stale data: {fr.detail}.",
                                observed_value=fr.data, expected_value="a fresh price", entities=ent,
                                evidence=[_ev(f"market_data:{obs.key}", "freshness", fr.detail, **fr.data)],
                            )
                        )
                    break
        result.mark_checked("valuation.unavailable")
        if snapshot.equity.value is None:
            result.candidates.append(
                FindingCandidate(
                    "valuation.unavailable", "valuation",
                    C if all(not p.price_available for p in snapshot.positions) else W,
                    "Portfolio valuation unavailable",
                    f"Portfolio equity is undefined: {snapshot.equity.reason}",
                    observed_value=None, expected_value="a defined equity value",
                    evidence=[_ev(f"portfolio:{state.experiment_id}", "valuation", str(snapshot.equity.reason))],
                )
            )
        result.finalize_status()
        result.summary = f"{len(snapshot.positions)} position(s) valued read-only"
        return result


# ---------------------------------------------------------------------------
# Risk / exposure
# ---------------------------------------------------------------------------


class RiskExposureCheck:
    check_id = "risk_exposure"

    def run(self, state, config, ctx) -> CheckResult:
        if state.portfolio is None:
            return CheckResult.not_evaluable(self.check_id, "no paper portfolio available")
        result = CheckResult(self.check_id, CheckStatus.HEALTHY)
        ref = f"portfolio:{state.experiment_id}"
        result.mark_checked("risk.invalid_config")
        if state.risk_config_error:
            result.candidates.append(
                FindingCandidate(
                    "risk.invalid_config", "risk", C, "Risk configuration is invalid",
                    f"The recorded risk configuration could not be loaded: {state.risk_config_error}",
                    observed_value=state.risk_config_error, expected_value="valid RiskLimits",
                    evidence=[_ev(ref, "risk_config", state.risk_config_error)],
                )
            )
        p = state.portfolio
        evaluated = False
        equity = p.equity
        if state.risk_limits is not None and equity > 0:
            evaluated = True
            limit = state.risk_limits.max_portfolio_exposure_pct
            ratio = p.total_exposure / (equity * limit)
            result.mark_checked("risk.portfolio_exposure")
            self._exposure(result, "risk.portfolio_exposure", ratio, config, ref, {},
                           f"Gross exposure is {ratio:.0%} of the {limit:.0%} portfolio ceiling",
                           dict(exposure=p.total_exposure, equity=equity, limit_pct=limit))
        prl = state.portfolio_risk_limits
        if prl is not None and equity > 0:
            if prl.max_symbol_exposure_pct is not None:
                evaluated = True
                for symbol in p.positions:
                    ratio = p.symbol_exposure(symbol) / (equity * prl.max_symbol_exposure_pct)
                    result.mark_checked("risk.symbol_exposure", symbol=symbol)
                    self._exposure(result, "risk.symbol_exposure", ratio, config, ref, {"symbol": symbol},
                                   f"{symbol} exposure is {ratio:.0%} of its ceiling",
                                   dict(symbol=symbol, limit_pct=prl.max_symbol_exposure_pct))
            if prl.max_concurrent_positions is not None:
                evaluated = True
                result.mark_checked("risk.concurrent_positions")
                n, cap = p.position_count, prl.max_concurrent_positions
                if n > cap or n == cap:
                    result.candidates.append(
                        FindingCandidate(
                            "risk.concurrent_positions", "risk", C if n > cap else W,
                            "Concurrent-position limit " + ("breached" if n > cap else "reached"),
                            f"{n} open position(s) against a limit of {cap}.",
                            observed_value=n, expected_value=f"< {cap}" if n == cap else f"<= {cap}",
                            evidence=[_ev(ref, "positions", "position count", count=n, limit=cap)],
                        )
                    )
        if state.risk_decisions is not None:
            evaluated = True
            result.mark_checked("risk.contradictory_decision")
            for d in state.risk_decisions:
                contradictory = (d.approved and d.final_approved_quantity > d.risk_quantity) or (
                    not d.approved and d.final_approved_quantity != 0
                )
                if contradictory:
                    result.candidates.append(
                        FindingCandidate(
                            "risk.contradictory_decision", "risk", C,
                            f"Contradictory risk decision for {d.symbol}",
                            "A RiskDecision's approval flag disagrees with its approved quantity. "
                            "An ordinary rejection is not a fault; this is.",
                            observed_value={"approved": d.approved, "final": d.final_approved_quantity,
                                            "risk_quantity": d.risk_quantity},
                            expected_value="approval consistent with quantity",
                            entities={"symbol": d.symbol, "signal_id": str(d.signal_id)},
                            evidence=[_ev(ref, "risk_decision", d.explanation)],
                        )
                    )
        if any(pos.side == PositionSide.SHORT for pos in p.positions.values()):
            result.notes.append(
                "short positions are open: short capital/margin mechanics are NOT_MODELED; "
                "this check makes no margin-safety claim for them"
            )
        if not evaluated and not state.risk_config_error:
            return CheckResult.not_evaluable(self.check_id, "no risk limits or decisions available to compare against")
        result.finalize_status()
        result.summary = "exposure compared with configured limits"
        return result

    @staticmethod
    def _exposure(result, rule, ratio, cfg, ref, ent, title, data) -> None:
        if ratio > 1.0 + 1e-9:
            sev = C
        elif ratio >= cfg.exposure_warning_ratio - 1e-9:
            sev = W
        else:
            return
        result.candidates.append(
            FindingCandidate(
                rule, "risk", sev, title + (" (above ceiling)" if sev == C else " (at/near ceiling)"),
                "Existing exposure is at or beyond a configured ceiling.",
                observed_value=round(ratio, 4), expected_value=f"< {cfg.exposure_warning_ratio}",
                entities=ent, evidence=[_ev(ref, "exposure", title, ratio=round(ratio, 4), **data)],
            )
        )


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------


class DrawdownCheck:
    check_id = "portfolio_drawdown"
    RULE = "drawdown.portfolio"

    def run(self, state, config, ctx) -> CheckResult:
        curve = state.equity_curve
        if curve is None or len(curve) == 0:
            return CheckResult.not_evaluable(self.check_id, "no equity history available")
        if len(curve) < config.drawdown_min_points:
            return CheckResult.not_evaluable(
                self.check_id, f"only {len(curve)} equity observation(s); need {config.drawdown_min_points}"
            )
        if curve.isna().any() or (curve <= 0).any():
            return CheckResult.not_evaluable(self.check_id, "equity history contains missing or non-positive values")
        dd = drawdown_curve(curve)
        current, worst = float(-dd.iloc[-1]), float(-dd.min())
        result = CheckResult(self.check_id, CheckStatus.HEALTHY)
        result.mark_checked(self.RULE)
        ref = f"equity_curve:{state.experiment_id}"
        if current >= config.drawdown_warning:
            result.candidates.append(
                FindingCandidate(
                    self.RULE, "drawdown", C if current >= config.drawdown_critical else W,
                    f"Portfolio drawdown {current:.1%}",
                    f"Current equity is {current:.1%} below its running peak (worst so far {worst:.1%}).",
                    observed_value=round(current, 6),
                    expected_value=f"< {config.drawdown_warning:.0%} (warning) / {config.drawdown_critical:.0%} (critical)",
                    evidence=[_ev(ref, "drawdown", "current vs peak", current=current, worst=worst,
                                  points=len(curve), peak=float(curve.cummax().iloc[-1]), latest=float(curve.iloc[-1]))],
                )
            )
        result.finalize_status()
        result.summary = f"current drawdown {current:.2%} over {len(curve)} observations"
        return result


# ---------------------------------------------------------------------------
# Execution / fill consistency
# ---------------------------------------------------------------------------


def _order_key(o) -> tuple:
    return (str(o.signal_id), o.side.value, o.symbol, pd.Timestamp(o.timestamp).isoformat())


class ExecutionConsistencyCheck:
    check_id = "execution_consistency"

    def run(self, state, config, ctx) -> CheckResult:
        have_records = state.execution is not None
        have_trades = state.trades is not None
        have_recon = state.reconciliations is not None
        if not (have_records or have_trades or have_recon):
            return CheckResult.not_evaluable(self.check_id, "no order/fill/trade records available")
        result = CheckResult(self.check_id, CheckStatus.HEALTHY)
        ref = f"execution:{state.experiment_id}"

        if have_records:
            rec = state.execution
            keys = {_order_key(o) for o in rec.orders}
            seen: dict[tuple, int] = {}
            for rule in ("execution.unknown_order", "execution.duplicate_fill",
                         "execution.invalid_chronology", "execution.quantity_exceeds_approved"):
                result.mark_checked(rule)
            for f in rec.fills:
                k = _order_key(f.order)
                seen[k] = seen.get(k, 0) + 1
                if rec.orders and k not in keys:
                    result.candidates.append(self._c("execution.unknown_order", C, "Fill refers to an unknown order",
                        "A fill's order is not among the recorded orders.", k, "a recorded order", ref,
                        {"signal_id": k[0], "symbol": k[2]}))
                if f.fill_timestamp is not None and pd.Timestamp(f.fill_timestamp) < pd.Timestamp(f.order.timestamp):
                    result.candidates.append(self._c("execution.invalid_chronology", C, "Fill precedes its order",
                        "fill_timestamp is earlier than the order timestamp.", str(f.fill_timestamp),
                        f">= {f.order.timestamp}", ref, {"signal_id": k[0], "symbol": k[2]}))
                approved = (rec.approved_quantity_by_signal_id or {}).get(str(f.order.signal_id))
                if approved is not None and f.order.quantity > approved + 1e-9:
                    result.candidates.append(self._c("execution.quantity_exceeds_approved", C,
                        "Executed quantity exceeds approved quantity",
                        "Execution must never exceed what Risk approved.", f.order.quantity, f"<= {approved}", ref,
                        {"signal_id": k[0], "symbol": k[2]}))
            for k, n in seen.items():
                if n > 1:
                    result.candidates.append(self._c("execution.duplicate_fill", C, "Duplicate fill identity",
                        f"{n} fills share one order identity.", n, 1, ref, {"signal_id": k[0], "symbol": k[2]}))

        if have_trades:
            result.mark_checked("trade.invalid_chronology")
            result.mark_checked("trade.unknown_entry_signal")
            signal_ids = {str(s.id) for s in state.signals} if state.signals is not None else None
            for t in state.trades:
                ent = {"entry_signal_id": str(t.entry_signal_id)}
                if pd.Timestamp(t.exit_time) < pd.Timestamp(t.entry_time):
                    result.candidates.append(self._c("trade.invalid_chronology", C, "Trade exits before it enters",
                        "exit_time is earlier than entry_time.", str(t.exit_time), f">= {t.entry_time}", ref, ent))
                if signal_ids is not None and str(t.entry_signal_id) not in signal_ids:
                    result.candidates.append(self._c("trade.unknown_entry_signal", C,
                        "Trade references an unknown signal",
                        "entry_signal_id is not among the experiment's recorded signals.",
                        str(t.entry_signal_id), "a recorded signal id", ref, ent))

        if have_recon:
            result.mark_checked("reconciliation.material_discrepancy")
            for r in state.reconciliations:
                if r.quantity_shortfall > 0 or abs(r.price_slippage_pct) > config.reconciliation_slippage_warning_pct:
                    result.candidates.append(self._c(
                        "reconciliation.material_discrepancy", W, "Fill reconciliation discrepancy",
                        "The reconciliation service reports a quantity shortfall or material slippage.",
                        {"shortfall": r.quantity_shortfall, "slippage_pct": r.price_slippage_pct},
                        f"slippage <= {config.reconciliation_slippage_warning_pct}", ref,
                        {"symbol": r.symbol, "order": r.real_broker_order_id}))
        result.finalize_status()
        result.summary = "orders/fills/trades inspected for provable inconsistencies"
        if not have_records:
            result.notes.append("order/fill records are not persisted by the registry; only trades were inspected")
        return result

    @staticmethod
    def _c(rule, sev, title, desc, observed, expected, ref, ent) -> FindingCandidate:
        return FindingCandidate(rule, "execution", sev, title, desc, observed_value=observed,
                                expected_value=expected, entities=ent,
                                evidence=[_ev(ref, "execution_record", title, **{k: str(v) for k, v in ent.items()})])


# ---------------------------------------------------------------------------
# Strategy / model compatibility
# ---------------------------------------------------------------------------


class StrategyModelCompatibilityCheck:
    check_id = "strategy_model_compat"

    def __init__(self, resolver: Callable[[str], type] = get_strategy_class) -> None:
        self._resolve = resolver

    def run(self, state, config, ctx) -> CheckResult:
        spec = state.spec
        if spec is None:
            return CheckResult.not_evaluable(self.check_id, "no experiment spec: no active strategy/model context")
        result = CheckResult(self.check_id, CheckStatus.HEALTHY)
        ref = f"experiment_spec:{state.experiment_id}"
        ent = {"strategy": spec.strategy_name}
        result.mark_checked("strategy.missing_implementation", **ent)
        try:
            cls = self._resolve(spec.strategy_name)
        except KeyError:
            result.candidates.append(FindingCandidate(
                "strategy.missing_implementation", "strategy", C,
                f"Strategy {spec.strategy_name!r} is not registered",
                "The experiment's strategy cannot be resolved through the StrategyRegistry.",
                observed_value=None, expected_value="a registered strategy", entities=ent,
                evidence=[_ev(ref, "spec", "strategy not registered", strategy=spec.strategy_name)]))
            cls = None
        if cls is not None:
            result.mark_checked("strategy.version_drift", **ent)
            try:
                if not spec.verify_strategy_version():
                    result.candidates.append(FindingCandidate(
                        "strategy.version_drift", "strategy", W,
                        f"Strategy {spec.strategy_name!r} source changed since the experiment",
                        "The registered class no longer hashes to the experiment's recorded strategy_version.",
                        observed_value="hash differs", expected_value=spec.strategy_version, entities=ent,
                        evidence=[_ev(ref, "spec", "version hash mismatch", recorded=spec.strategy_version)]))
            except Exception as exc:  # noqa: BLE001
                result.notes.append(f"strategy version could not be verified: {type(exc).__name__}")
        if state.models:
            for model_id, info in state.models.items():
                e = {"model_id": model_id}
                for rule in ("model.unavailable", "model.artifact_hash_mismatch", "model.context_mismatch"):
                    result.mark_checked(rule, **e)
                if info is None:
                    result.candidates.append(FindingCandidate(
                        "model.unavailable", "model", C, f"Model {model_id} is unavailable",
                        "The strategy requires a model that the model registry does not have.",
                        observed_value=None, expected_value="a registered model", entities=e,
                        evidence=[_ev(ref, "model", "not in registry", model_id=model_id)]))
                    continue
                if info.recomputed_artifact_hash and info.recomputed_artifact_hash != info.artifact_hash:
                    result.candidates.append(FindingCandidate(
                        "model.artifact_hash_mismatch", "model", C, f"Model {model_id} artifact hash mismatch",
                        "The artifact on disk no longer matches its recorded provenance hash.",
                        observed_value=info.recomputed_artifact_hash, expected_value=info.artifact_hash, entities=e,
                        evidence=[_ev(ref, "model", "artifact hash mismatch", model_id=model_id)]))
                if (info.symbol and info.symbol != spec.symbol) or (info.interval and info.interval != spec.interval.value):
                    result.candidates.append(FindingCandidate(
                        "model.context_mismatch", "model", W, f"Model {model_id} trained for a different context",
                        "The model's symbol/interval differ from the experiment's.",
                        observed_value={"symbol": info.symbol, "interval": info.interval},
                        expected_value={"symbol": spec.symbol, "interval": spec.interval.value}, entities=e,
                        evidence=[_ev(ref, "model", "context mismatch", model_id=model_id)]))
        elif state.models_unavailable_reason:
            result.notes.append(f"model checks skipped: {state.models_unavailable_reason}")
        result.finalize_status()
        result.summary = "strategy resolved; model provenance compared" if state.models else "strategy resolved"
        return result


def default_checks() -> list:
    return [
        MarketDataFreshnessCheck(),
        MarketDataIntegrityCheck(),
        PortfolioValuationCheck(),
        RiskExposureCheck(),
        DrawdownCheck(),
        ExecutionConsistencyCheck(),
        StrategyModelCompatibilityCheck(),
    ]
