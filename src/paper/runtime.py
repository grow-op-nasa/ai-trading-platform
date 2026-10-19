"""The paper-trading decision cycle (Sprint 18, ADR-0053).

    gate (recover unresolved intents, reconcile) -> validated data -> closed decision bar -> idempotency key
    -> registered strategy -> signal at the decision bar -> risk engine -> DURABLE INTENT -> paper broker
    (ledger, keyed by client order id) -> portfolio -> reconciliation -> monitoring -> cycle record

Authority is preserved: the strategy proposes, risk authorises/sizes/rejects, execution performs only the approved
intent, the portfolio records the result, reconciliation verifies it, monitoring evaluates persisted state. No LLM
is involved anywhere. Crash/retry guarantees are described in ADR-0053 (at-most-once ledger execution per client order
id; recoverable by lookup; no exactly-once claim across the two SQLite files).
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from src.backtesting.execution_model import ExecutionModel
from src.data.exceptions import DataProviderError, DataValidationError
from src.execution.models import Order, OrderSide
from src.execution.portfolio_sync import apply_fill_to_portfolio
from src.paper import bars
from src.paper.config import PaperRunConfig
from src.strategies.history import assess_history, requirement_of
from src.paper.evaluation import StrategyResolutionError, assess_risk, prepare_bars, resolve_strategy, safe_error_text, evaluate_at_bar, jsonable as _jsonable, signal_to_record
from src.paper.faults import BrokerUncertain, NoFaults
from src.paper.gateway import (
    ADAPTER_NAME,
    BrokerRejected,
    PaperBrokerGateway,
    fill_from_record,
    signal_from_record,
)
from src.paper.models import (
    CheckStatus,
    CycleRecord,
    CycleStatus,
    IntentRecord,
    IntentState,
    NON_TERMINAL_CYCLE,
    PreflightCheck,
    PreflightReport,
    ProposalRecord,
    ProposalStatus,
    ReconStatus,
)
from src.paper.reconcile import portfolio_state, reconcile, states_match
from src.paper.store import DEFAULT_DB_PATH, PaperStore, PaperStoreError
from src.risk.portfolio_risk import PortfolioRiskEngine
from src.signals.models import Signal, SignalDirection
from src.utils.hashing import dataframe_fingerprint

_NO_RETRY = (CycleStatus.NO_ACTION, CycleStatus.RISK_REJECTED, CycleStatus.ORDER_REJECTED,
             CycleStatus.ORDER_FILLED, CycleStatus.RECONCILIATION_REQUIRED,
             CycleStatus.AWAITING_APPROVAL, CycleStatus.PROPOSAL_REJECTED)     # EXPIRED/INVALIDATED may be re-evaluated
class PaperSetupError(RuntimeError):
    """The target cannot be run at all (missing registry/account/strategy); nothing was recorded."""


class PaperLockedError(RuntimeError):
    """Another paper run holds the lock for this account."""


class PaperProposalError(RuntimeError):
    """A proposal decision was refused. `code` is machine-readable; nothing was submitted to the paper broker."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


safe_error = safe_error_text


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


class PaperTradingRuntime:
    def __init__(
        self,
        config: PaperRunConfig,
        *,
        registry_path: Path | str,
        store_path: Path | str = DEFAULT_DB_PATH,
        market_data: Any,
        monitor_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        faults: Any = None,
        calendar: Any = None,
        allowed_strategy_prefixes: tuple[str, ...] = ("src.strategies",),
        approval_required: bool = True,
        proposal_ttl_seconds: int = 1800,
        snapshots: Any = None,
    ) -> None:
        config.validate()
        self._snapshots = snapshots          # optional src.snapshots.SnapshotStore: when set, each cycle's dataset is preserved
        self._config = config
        self._registry_path = Path(registry_path)
        self._store_path = Path(store_path)
        self._md = market_data
        self._monitor_factory = monitor_factory
        self._clock = clock
        self._faults = faults or NoFaults()
        if calendar is None:
            from src.calendar.nyse import NYSECalendar
            calendar = NYSECalendar()
        self._cal = calendar
        self._prefixes = allowed_strategy_prefixes
        # Sprint 20 (ADR-0056): by default a risk-approved action becomes a persisted PROPOSAL that a human must approve.
        # `False` is the Sprint 18 immediate path, reachable only by passing it explicitly (tests/fixtures).
        self._approval_required = bool(approval_required)
        self._ttl = int(proposal_ttl_seconds)
        self._memo: tuple[datetime, Any] | None = None
        self._risk = PortfolioRiskEngine(config.risk_limits(), config.portfolio_limits())
        self._stop = config.stop_policy()
        self._exec = ExecutionModel(config.execution_config())

    # ================================================================== setup helpers
    @property
    def config(self) -> PaperRunConfig:
        return self._config

    def _registry(self):
        if not self._registry_path.is_file():
            raise PaperSetupError(f"experiment registry not found at {self._registry_path}; it is never created implicitly")
        from src.experiments.registry import ExperimentRegistry
        return ExperimentRegistry(self._registry_path)

    def _portfolio(self, registry):
        portfolio = registry.get_portfolio(self._config.experiment_id)
        if portfolio is None:
            raise PaperSetupError(f"experiment {self._config.experiment_id} has no saved paper portfolio; "
                                  "the runtime never creates or invents one")
        return portfolio

    def _open_store(self) -> PaperStore:
        try:
            return PaperStore.open_existing(self._store_path)
        except PaperStoreError as exc:
            raise PaperSetupError(str(exc)) from exc

    def _require_account(self, store: PaperStore) -> None:
        if store.get_account(self._config.experiment_id) is None:
            raise PaperSetupError(f"no paper account initialised for experiment {self._config.experiment_id}; "
                                  "run `python -m src.cli paper init` explicitly")

    def resolve_strategy(self):
        """(instance, strategy_hash) via the ordinary production registry only (shared with the scanner)."""
        try:
            return resolve_strategy(self._config.strategy_name, self._config.symbol, self._config.strategy_params, self._prefixes)
        except StrategyResolutionError as exc:
            raise PaperSetupError(str(exc)) from exc

    @contextmanager
    def _lock(self):
        from src.monitoring.scheduler import SchedulerLock, SchedulerLockError
        lock = SchedulerLock(self._store_path.parent, f"paper-{self._config.experiment_id}")
        try:
            lock.acquire()
        except SchedulerLockError as exc:
            raise PaperLockedError(str(exc)) from exc
        try:
            yield
        finally:
            lock.release()

    def _gateway(self, store: PaperStore) -> PaperBrokerGateway:
        return PaperBrokerGateway(store, self._config.experiment_id, self._faults)

    # ================================================================== account initialisation (explicit)
    def initialize_account(self, label: str) -> dict[str, Any]:
        """Record the paper broker's opening baseline from the CURRENT saved portfolio. Explicit operator action."""
        if not label.strip():
            raise PaperSetupError("a non-empty --label is required")
        registry = self._registry()
        portfolio = self._portfolio(registry)
        if portfolio.closed_positions:
            pass  # closed history is irrelevant to the baseline; only cash and open positions matter
        store = PaperStore.create(self._store_path)
        positions = [{"symbol": p.symbol, "quantity": p.quantity, "entry_price": p.entry_price,
                      "entry_signal_id": str(p.entry_signal_id)} for p in portfolio.positions.values()]
        try:
            store.init_account(self._config.experiment_id, label.strip(), portfolio.cash, positions,
                               self._clock().isoformat())
        except PaperStoreError as exc:
            raise PaperSetupError(str(exc)) from exc
        return {"experiment_id": self._config.experiment_id, "baseline_cash": portfolio.cash,
                "baseline_positions": positions, "label": label.strip()}

    # ================================================================== preflight (read-only)
    def preflight(self, check_data: bool = True) -> PreflightReport:
        rep = PreflightReport()
        add = lambda n, s, d="": rep.checks.append(PreflightCheck(n, s, d))  # noqa: E731
        add("configuration", CheckStatus.PASS, f"valid; config hash {self._config.identity_hash()[:16]}")
        add("paper_only_execution", CheckStatus.PASS, f"execution_mode='paper'; adapter hard-bound to {ADAPTER_NAME}")
        try:
            from src.execution.engine import PaperBroker
            ok = PaperBrokerGateway.__module__ == "src.paper.gateway" and PaperBroker.__module__ == "src.execution.engine"
            add("paper_broker", CheckStatus.PASS if ok else CheckStatus.FAIL, "PaperBroker instantiable (in-memory)")
        except Exception as exc:  # noqa: BLE001
            add("paper_broker", CheckStatus.FAIL, safe_error(exc))
        # registry / portfolio
        registry = None
        if not self._registry_path.is_file():
            add("registry", CheckStatus.FAIL, f"registry not found at {self._registry_path}")
        else:
            try:
                from src.experiments.registry import ExperimentRegistry
                registry = ExperimentRegistry(self._registry_path)
                add("registry", CheckStatus.PASS, str(self._registry_path))
            except Exception as exc:  # noqa: BLE001
                add("registry", CheckStatus.FAIL, safe_error(exc))
        portfolio = None
        if registry is not None:
            portfolio = registry.get_portfolio(self._config.experiment_id)
            add("portfolio", CheckStatus.PASS if portfolio is not None else CheckStatus.FAIL,
                f"experiment {self._config.experiment_id}: " + ("saved paper portfolio found" if portfolio is not None
                                                                else "no saved paper portfolio"))
        # strategy
        try:
            inst, shash = self.resolve_strategy()
            add("strategy", CheckStatus.PASS, f"{self._config.strategy_name} resolved via production registry; hash {shash[:16]}")
            lookup = requirement_of(inst)
            if not lookup.usable:
                add("strategy_history_requirement", CheckStatus.FAIL, lookup.detail)
            else:
                need, cap = lookup.requirement.min_bars, (self._config.lookback_days * 5) // 7 + 1
                if need > cap:
                    add("strategy_history_requirement", CheckStatus.FAIL,
                        f"needs {need} complete bars but lookback_days={self._config.lookback_days} holds at most {cap} sessions")
                else:
                    add("strategy_history_requirement", CheckStatus.PASS, lookup.requirement.explain())
        except PaperSetupError as exc:
            add("strategy", CheckStatus.FAIL, str(exc))
        add("instrument_and_timeframe", CheckStatus.PASS, f"{self._config.symbol} {self._config.interval} (daily bars)")
        add("risk_and_execution_settings", CheckStatus.PASS,
            f"risk limits, ATR stop and execution costs valid; timing={self._config.execution['timing']}")
        # persistence
        store = None
        if not self._store_path.is_file():
            add("paper_database", CheckStatus.FAIL, f"not found at {self._store_path}; run `paper init`")
        else:
            try:
                store = PaperStore.open_existing(self._store_path)
                add("paper_database", CheckStatus.PASS, f"schema v{store.schema_version()} at {self._store_path}")
            except PaperStoreError as exc:
                add("paper_database", CheckStatus.FAIL, str(exc))
        if store is not None:
            account = store.get_account(self._config.experiment_id)
            add("paper_account", CheckStatus.PASS if account else CheckStatus.FAIL,
                "initialised" if account else "not initialised; run `paper init`")
            if account:
                unresolved = store.list_intents(self._config.experiment_id, unresolved_only=True)
                add("unresolved_intents", CheckStatus.BLOCKED if unresolved else CheckStatus.PASS,
                    f"{len(unresolved)} unresolved intent(s): " + ", ".join(i.client_order_id for i in unresolved)
                    if unresolved else "none")
                if portfolio is not None:
                    try:
                        rec = reconcile(store, self._gateway(store), self._config.experiment_id, portfolio,
                                        self._clock(), persist=False)
                        st = {ReconStatus.MATCH: CheckStatus.PASS, ReconStatus.PENDING: CheckStatus.BLOCKED,
                              ReconStatus.MISMATCH: CheckStatus.BLOCKED}[rec.status]
                        add("reconciliation", st, rec.status.value + (f": {len(rec.discrepancies)} discrepancy(ies)"
                                                                      if rec.discrepancies else ""))
                    except Exception as exc:  # noqa: BLE001
                        add("reconciliation", CheckStatus.NOT_EVALUABLE, safe_error(exc))
            import os
            add("persistence_writable", CheckStatus.PASS if os.access(self._store_path, os.W_OK) else CheckStatus.FAIL,
                "paper database is writable" if os.access(self._store_path, os.W_OK) else "paper database is read-only")
        # market data
        if not check_data:
            add("market_data", CheckStatus.NOT_EVALUABLE, "skipped (--no-data-check)")
        else:
            add(*self._data_check())
        # alerts / monitoring availability (informational, never trading-critical)
        try:
            from src.monitoring.alerts.config import AlertConfig
            cfg = AlertConfig.from_env()
            if not cfg.enabled:
                add("alerting", CheckStatus.PASS, "disabled (events are not recorded)")
            else:
                problems = cfg.problems()
                add("alerting", CheckStatus.FAIL if problems else CheckStatus.PASS,
                    "; ".join(problems) if problems else "enabled and valid")
        except Exception as exc:  # noqa: BLE001
            add("alerting", CheckStatus.NOT_EVALUABLE, safe_error(exc))
        add("monitoring", CheckStatus.PASS if self._monitor_factory else CheckStatus.NOT_EVALUABLE,
            "monitor factory configured" if self._monitor_factory else "no monitoring service configured")
        # duplicate-runtime protection
        try:
            from src.monitoring.scheduler import SchedulerLock, SchedulerLockError
            lock = SchedulerLock(self._store_path.parent, f"paper-{self._config.experiment_id}")
            if self._store_path.parent.exists():
                try:
                    lock.acquire()
                    lock.release()
                    add("duplicate_run_protection", CheckStatus.PASS, "no other paper run holds the lock")
                except SchedulerLockError as exc:
                    add("duplicate_run_protection", CheckStatus.BLOCKED, str(exc))
            else:
                add("duplicate_run_protection", CheckStatus.NOT_EVALUABLE, "paper directory does not exist")
        except Exception as exc:  # noqa: BLE001
            add("duplicate_run_protection", CheckStatus.NOT_EVALUABLE, safe_error(exc))
        return rep

    def _data_check(self):
        try:
            ds = self._fetch_dataset(remember=True)
        except Exception as exc:  # noqa: BLE001
            return ("market_data", CheckStatus.FAIL, safe_error(exc))
        return ("market_data", CheckStatus.PASS, f"{len(ds.candles)} bars via {ds.provider}, latest {ds.dataset_end}")

    def _fetch_dataset(self, *, remember: bool = False, reuse: bool = False):
        """`remember`: keep the result for ONE immediately following run in this operation (preflight -> run). `reuse`:
        consume that memo if it is under two minutes old. Approval revalidation never reuses (always fresh)."""
        now = self._clock()
        if reuse and self._memo is not None:
            at, ds = self._memo
            self._memo = None
            if 0 <= (now - at).total_seconds() < 120:
                return ds
        end = now.date()
        start = end - timedelta(days=self._config.lookback_days)
        ds = self._md.get_dataset(self._config.symbol, start=start, end=end,
                                  interval=self._config.interval_enum(), use_cache=False)
        if remember:
            self._memo = (now, ds)
        return ds

    # ================================================================== public cycle API
    def run_cycle(self) -> CycleRecord:
        store = self._open_store()
        self._require_account(store)
        registry = self._registry()
        self._portfolio(registry)
        instance, shash = self.resolve_strategy()
        with self._lock():
            return self._run_locked(store, registry, instance, shash)

    def recover(self) -> list[CycleRecord]:
        store = self._open_store()
        self._require_account(store)
        registry = self._registry()
        with self._lock():
            return self._recover_locked(store, registry)

    def reconcile_now(self):
        store = self._open_store()
        self._require_account(store)
        registry = self._registry()
        portfolio = self._portfolio(registry)
        with self._lock():
            return reconcile(store, self._gateway(store), self._config.experiment_id, portfolio, self._clock())

    # ================================================================== the cycle
    def _new_record(self, shash: str | None) -> CycleRecord:
        c = self._config
        return CycleRecord(cycle_id=uuid.uuid4().hex, experiment_id=c.experiment_id, status=CycleStatus.STARTED,
                           started_at=self._clock().isoformat(), strategy_name=c.strategy_name, strategy_hash=shash,
                           config_hash=c.identity_hash(), symbol=c.symbol, interval=c.interval,
                           provenance={"adapter": ADAPTER_NAME, "execution_mode": "paper",
                                       "execution": c.execution_config().describe(),
                                       "stop": c.stop_policy().config, "risk": c.risk})

    def _persist(self, store: PaperStore, rec: CycleRecord) -> None:
        if not store.insert_cycle(rec):
            store.save_cycle(rec)

    def _finish(self, store: PaperStore, rec: CycleRecord, status: CycleStatus, code: str | None = None,
                reason: str | None = None, persist: bool = True) -> CycleRecord:
        rec.status, rec.reason_code, rec.reason = status, code, (reason[:400] if reason else None)
        rec.finished_at = self._clock().isoformat()
        if persist:
            if self._monitor_wanted(rec):
                rec.monitoring = {"outcome": "PENDING", "status": "PENDING"}        # durable BEFORE the run: crash-recoverable
            elif rec.monitoring is None:
                rec.monitoring = self._monitor_skipped(rec)
            self._persist(store, rec)
            if (rec.monitoring or {}).get("outcome") == "PENDING":
                self._monitor(rec)
                self._persist(store, rec)                                          # the real result, only after the run
        return rec

    def _run_locked(self, store: PaperStore, registry, instance, shash: str) -> CycleRecord:
        cfg = self._config
        rec = self._new_record(shash)
        try:
            # ---- gate: resolve anything left over before taking any new risk
            self._recover_locked(store, registry)
            self._expire_due(store)
            pending = store.list_proposals(cfg.experiment_id, (ProposalStatus.PENDING, ProposalStatus.APPROVING), limit=1)
            if pending:                                 # a decision is already awaiting the human: replay it, never stack another
                waiting = store.get_cycle(pending[0].cycle_id)
                if waiting is not None:
                    waiting.replayed = True
                    return waiting
            unresolved = store.list_intents(cfg.experiment_id, unresolved_only=True)
            if unresolved:
                last = store.latest_reconciliation(cfg.experiment_id)
                if last is not None and last.status is ReconStatus.MISMATCH:
                    rec.reconciliation_id, rec.reconciliation_status = last.recon_id, last.status.value
                    return self._finish(store, rec, CycleStatus.RECONCILIATION_REQUIRED, "RECONCILIATION_MISMATCH",
                                        f"{len(unresolved)} intent(s) cannot be applied because the portfolio matches "
                                        "neither the pre- nor post-fill broker state; a human must resolve it")
                return self._finish(store, rec, CycleStatus.AWAITING_EXECUTION, "UNRESOLVED_INTENT",
                                    f"{len(unresolved)} execution intent(s) unresolved: "
                                    + ", ".join(i.client_order_id for i in unresolved))
            portfolio = self._portfolio(registry)
            recon = self._gate_reconciliation(store, portfolio, rec.cycle_id)
            rec.reconciliation_id, rec.reconciliation_status = recon.recon_id, recon.status.value
            if recon.status is not ReconStatus.MATCH:
                return self._finish(store, rec, CycleStatus.RECONCILIATION_REQUIRED, "RECONCILIATION_" + recon.status.value,
                                    "paper broker ledger and portfolio disagree; no new risk until understood")

            # ---- data
            try:
                ds = self._fetch_dataset(reuse=True)
            except (DataProviderError, DataValidationError) as exc:
                code = "DATA_INVALID" if isinstance(exc, DataValidationError) else "DATA_UNAVAILABLE"
                return self._finish(store, rec, CycleStatus.BLOCKED, code, safe_error(exc))
            prep = prepare_bars(ds.candles, self._cal, self._clock())
            if prep.kind == "NO_COMPLETE_BAR":
                return self._finish(store, rec, CycleStatus.BLOCKED, "NO_COMPLETE_BAR", prep.detail)
            if prep.kind == "CALENDAR_UNSUPPORTED":
                return self._finish(store, rec, CycleStatus.BLOCKED, "CALENDAR_UNSUPPORTED", prep.detail)
            if prep.kind == "DATA_STALE":
                return self._finish(store, rec, CycleStatus.BLOCKED, "DATA_STALE", prep.detail)
            if prep.kind == "AWAITING_PROVIDER_BAR":
                rec.status, rec.reason_code = CycleStatus.NO_NEW_BAR, "AWAITING_PROVIDER_BAR"
                rec.reason = prep.detail
                rec.finished_at = self._clock().isoformat()
                return rec                                               # not persisted: nothing happened
            complete, dropped, decision_bar = prep.complete, prep.dropped, prep.decision_bar
            rec.decision_bar = decision_bar.isoformat()
            history = assess_history(requirement_of(instance), int(len(complete)))
            snapshot_ref = None
            if self._snapshots is not None:
                try:
                    from src.snapshots.meta import snapshot_meta
                    snap = self._snapshots.put(ds.candles, snapshot_meta(ds, cfg.symbol, cfg.interval))
                except Exception as exc:  # noqa: BLE001 - an unpreserved decision input blocks the cycle before any intent
                    return self._finish(store, rec, CycleStatus.BLOCKED, "SNAPSHOT_FAILED",
                                        f"the market-data snapshot could not be stored and verified: {safe_error(exc)}")
                snapshot_ref = {"status": "CAPTURED", "snapshot_id": snap.snapshot_id}
            rec.dataset = {
                "provider": ds.provider, "dataset_hash": ds.content_hash,
                "complete_bars_hash": dataframe_fingerprint(complete), "bars_used": int(len(complete)),
                "dataset_start": ds.dataset_start.isoformat(), "dataset_end": ds.dataset_end.isoformat(),
                "dropped_incomplete_bars": [pd.Timestamp(t).isoformat() for t in dropped],
                "validation_clean": ds.validation_report.is_clean, "gap_count": ds.validation_report.gaps.count,
                "session_policy": ds.session_policy.value, "session_timezone": ds.session_timezone,
                "cache_used": False, "history": history.to_dict(),
                "snapshot": snapshot_ref or {"status": "NOT_CAPTURED"}}
            if not history.satisfied:
                code = "INSUFFICIENT_HISTORY" if history.status == "DECLARED" else "HISTORY_REQUIREMENT_UNKNOWN"
                return self._finish(store, rec, CycleStatus.BLOCKED, code,
                                    f"{history.available} complete bars available; {history.explanation}")

            # ---- identity / duplicate protection
            key = _sha(cfg.strategy_name, shash, rec.config_hash, str(cfg.experiment_id), cfg.symbol, cfg.interval,
                       rec.decision_bar)
            rec.idempotency_key = key
            existing = store.get_cycle_by_key(key)
            if existing is not None and existing.status in _NO_RETRY:
                dup = existing
                dup.replayed = True
                return dup
            if existing is not None:
                rec = existing                                          # retry an unfinished/blocked/failed cycle
            acted = store.intent_for_bar(cfg.experiment_id, cfg.symbol, rec.decision_bar)
            if acted is not None and existing is None:
                out = CycleRecord.from_dict({**rec.to_dict()})
                out.status, out.reason_code = CycleStatus.NO_NEW_BAR, "BAR_ALREADY_ACTED"
                out.reason = (f"decision bar {rec.decision_bar} already has execution intent "
                              f"{acted.client_order_id} under a different input fingerprint")
                out.finished_at = self._clock().isoformat()
                return out
            rec.status = CycleStatus.STARTED
            self._persist(store, rec)

            # ---- strategy (complete bars only, nothing after the decision bar) -- shared with the scanner (src.paper.evaluation)
            ev = evaluate_at_bar(instance, complete, decision_bar, cfg.symbol)
            if ev.outcome == "STRATEGY_ERROR":
                return self._finish(store, rec, CycleStatus.FAILED, "STRATEGY_ERROR", ev.detail)
            if ev.outcome == "SYMBOL_MISMATCH":
                return self._finish(store, rec, CycleStatus.FAILED, "SIGNAL_SYMBOL_MISMATCH", ev.detail)
            if ev.outcome == "NO_SIGNAL":
                return self._finish(store, rec, CycleStatus.NO_ACTION, "NO_SIGNAL", ev.detail)
            if ev.outcome == "AMBIGUOUS":
                return self._finish(store, rec, CycleStatus.BLOCKED, "AMBIGUOUS_SIGNALS", ev.detail)
            signal = ev.signal
            rec.signal = signal_to_record(signal)

            # ---- risk (shared, read-only assessment)
            price = float(complete.loc[decision_bar, "close"])
            portfolio = self._portfolio(registry)
            rk = assess_risk(self._risk, self._stop, signal, complete, decision_bar, portfolio, price)
            if rk.kind == "STOP_UNAVAILABLE":
                rec.risk_decision = rk.record
                return self._finish(store, rec, CycleStatus.RISK_REJECTED, "STOP_UNAVAILABLE", rk.reason)
            if rk.kind == "ENGINE_ERROR":
                return self._finish(store, rec, CycleStatus.FAILED, "RISK_ENGINE_ERROR", rk.reason)
            decision, stop_price = rk.decision, rk.stop_price
            rec.risk_decision = rk.record
            if rk.kind == "ALREADY_FLAT":
                return self._finish(store, rec, CycleStatus.NO_ACTION, "ALREADY_FLAT", rk.reason)
            if rk.kind == "REJECTED":
                return self._finish(store, rec, CycleStatus.RISK_REJECTED, rk.code, rk.reason)
            if rk.kind == "SHORT_NOT_MODELED":
                return self._finish(store, rec, CycleStatus.RISK_REJECTED, "SHORT_NOT_MODELED", rk.reason)

            # ---- durable intent BEFORE any execution side effect
            try:
                trade_intent = decision.to_trade_intent()
            except ValueError as exc:
                return self._finish(store, rec, CycleStatus.FAILED, "INTENT_INVALID", safe_error(exc))
            if signal.direction is SignalDirection.FLAT:
                position = portfolio.positions[cfg.symbol]
                side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
            else:
                side = OrderSide.BUY if signal.direction is SignalDirection.LONG else OrderSide.SELL
            quantity = float(trade_intent.quantity)          # <= risk-approved, enforced by to_trade_intent()
            approved_qty = float(decision.final_approved_quantity)
            if quantity > approved_qty + 1e-9:
                return self._finish(store, rec, CycleStatus.FAILED, "INTENT_INVALID", "quantity exceeds approval")
            intent = self._build_intent(rec, key, signal, side.value, quantity, approved_qty, price, stop_price)
            if self._approval_required:
                return self._propose(store, rec, intent, portfolio)
            rec.intent_id, rec.client_order_id = intent.intent_id, intent.client_order_id
            rec.status = CycleStatus.AWAITING_EXECUTION
            if not store.record_intent(intent, rec):
                return self._finish(store, rec, CycleStatus.BLOCKED, "INTENT_ALREADY_EXISTS",
                                    "an intent for this cycle/bar already exists; recovery will resolve it")
            self._faults.fire("after_intent_persisted")

            # ---- execute + apply + reconcile + monitor
            return self._execute_and_conclude(store, registry, intent, rec)
        except (PaperSetupError, PaperLockedError):
            raise
        except Exception as exc:  # noqa: BLE001
            return self._finish_failure(store, rec, exc)

    def _gate_reconciliation(self, store: PaperStore, portfolio, cycle_id: str):
        """Fresh reconciliation before any new risk. A healthy repeat of an unchanged MATCH is not re-persisted
        (so a no-op cycle mutates nothing); every change of status and every problem IS persisted."""
        fresh = reconcile(store, self._gateway(store), self._config.experiment_id, portfolio, self._clock(),
                          cycle_id, persist=False)
        last = store.latest_reconciliation(self._config.experiment_id)
        if last is not None and fresh.status is ReconStatus.MATCH and last.status is ReconStatus.MATCH:
            return last
        return store.add_reconciliation(fresh)

    def _finish_failure(self, store: PaperStore, rec: CycleRecord, exc: BaseException) -> CycleRecord:
        try:
            return self._finish(store, rec, CycleStatus.FAILED, "INFRASTRUCTURE_ERROR", safe_error(exc))
        except Exception:  # noqa: BLE001  the failure record itself could not be written
            rec.status, rec.reason_code, rec.reason = CycleStatus.FAILED, "INFRASTRUCTURE_ERROR", safe_error(exc)
            return rec

    # ================================================================== execution / application
    def _execute_and_conclude(self, store: PaperStore, registry, intent: IntentRecord, rec: CycleRecord) -> CycleRecord:
        gateway = self._gateway(store)
        fill_rec = gateway.lookup(intent.client_order_id)     # lookup BEFORE any (re)submission
        if fill_rec is None:
            signal = signal_from_record(intent.signal)
            ts = pd.Timestamp(signal.timestamp)
            order = Order(symbol=intent.symbol, side=OrderSide(intent.side), quantity=intent.quantity,
                          signal_id=signal.id, timestamp=ts)
            px = intent.reference_price
            candles = pd.DataFrame({"open": [px], "high": [px], "low": [px], "close": [px], "volume": [0.0]},
                                   index=pd.DatetimeIndex([ts], name="timestamp"))
            outcome = self._exec.simulate(order, candles)
            if not outcome.filled:
                return self._reject(store, intent, rec, "EXECUTION_UNAVAILABLE", str(outcome.reason))
            portfolio = self._portfolio(registry)
            if portfolio.cash + outcome.fill.cash_delta < -1e-9:
                return self._reject(store, intent, rec, "INSUFFICIENT_CASH", "fill (incl. fee) would drive cash negative")
            try:
                fill_rec = gateway.submit(intent, signal, outcome.fill)
            except BrokerRejected as exc:
                return self._reject(store, intent, rec, "BROKER_REJECTED", str(exc))
            except BrokerUncertain as exc:
                rec.fill = None
                return self._finish(store, rec, CycleStatus.AWAITING_EXECUTION, "BROKER_UNCERTAIN",
                                    safe_error(exc) + "; will look up by client order id before any retry")
        rec.fill = fill_rec.to_dict()
        state = self._apply_fill(store, registry, intent, fill_rec)
        portfolio = self._portfolio(registry)
        recon = reconcile(store, gateway, self._config.experiment_id, portfolio, self._clock(), rec.cycle_id)
        rec.reconciliation_id, rec.reconciliation_status = recon.recon_id, recon.status.value
        if state == "MISMATCH" or recon.status is not ReconStatus.MATCH:
            return self._finish(store, rec, CycleStatus.RECONCILIATION_REQUIRED, "RECONCILIATION_" + recon.status.value,
                                "portfolio does not agree with the paper broker ledger after execution")
        return self._finish(store, rec, CycleStatus.ORDER_FILLED, "FILLED",
                            f"{intent.side} {intent.quantity:g} {intent.symbol} @ {fill_rec.fill_price:g} "
                            f"(fee {fill_rec.fee:g})")

    def _reject(self, store, intent, rec, code, reason) -> CycleRecord:
        store.set_intent_state(intent.intent_id, IntentState.REJECTED, f"{code}: {reason}"[:300])
        return self._finish(store, rec, CycleStatus.ORDER_REJECTED, code, reason)

    def _apply_fill(self, store: PaperStore, registry, intent: IntentRecord, fill_rec) -> str:
        """Bring the portfolio in line with ONE recorded fill, exactly once. Returns APPLIED or MISMATCH."""
        gateway = self._gateway(store)
        portfolio = self._portfolio(registry)
        pstate = portfolio_state(portfolio)
        after = gateway.state(upto_seq=fill_rec.seq, include_upto=True)
        before = gateway.state(upto_seq=fill_rec.seq, include_upto=False)
        if states_match(pstate, after):                      # already reflected (e.g. crash after the save)
            store.set_intent_state(intent.intent_id, IntentState.APPLIED)
            return "APPLIED"
        if states_match(pstate, before):
            self._faults.fire("before_portfolio_save")
            apply_fill_to_portfolio(portfolio, fill_from_record(fill_rec), stop_price=intent.stop_price)
            registry.save_portfolio(self._config.experiment_id, portfolio)
            self._faults.fire("after_portfolio_save")
            store.set_intent_state(intent.intent_id, IntentState.APPLIED)
            return "APPLIED"
        return "MISMATCH"                                    # neither before nor after: unexplained, never overwritten

    # Statuses after which post-cycle monitoring runs. Monitoring evaluates persisted portfolio/operational state, so it
    # runs after a cycle that COMPLETED its evaluation even when nothing was traded (NO_ACTION). It is skipped, with the
    # reason recorded, when the cycle never evaluated anything (BLOCKED/FAILED/AWAITING_EXECUTION).
    MONITORED_STATUSES = frozenset({CycleStatus.NO_ACTION, CycleStatus.RISK_REJECTED, CycleStatus.ORDER_REJECTED,
                                    CycleStatus.ORDER_FILLED, CycleStatus.RECONCILIATION_REQUIRED})

    def _monitor_wanted(self, rec: CycleRecord) -> bool:
        if self._monitor_factory is None or rec.status not in self.MONITORED_STATUSES:
            return False
        return (rec.monitoring or {}).get("outcome") not in ("COMPLETED",)      # a completed step is never repeated

    def _monitor_skipped(self, rec: CycleRecord) -> dict[str, Any]:
        if self._monitor_factory is None:
            return {"outcome": "NOT_CONFIGURED", "status": "NOT_CONFIGURED",
                    "reason": "no monitoring service was configured for this run"}
        return {"outcome": "SKIPPED", "status": "SKIPPED",
                "reason": f"policy: monitoring runs only after a cycle that completed evaluation; this cycle ended {rec.status.value}"}

    def _monitor(self, rec: CycleRecord, *, late: bool = False) -> None:
        """Run the deterministic monitoring service ONCE and record the real outcome. Never changes execution truth."""
        attempts = int((rec.monitoring or {}).get("attempts", 0)) + 1
        try:
            self._faults.fire("before_monitoring")
            run = self._monitor_factory().run_once()
            rec.monitoring = {"outcome": "COMPLETED", "run_id": run.run_id,
                              "status": getattr(run.status, "value", str(run.status)), "persisted": bool(run.persisted),
                              "at": self._clock().isoformat(), "attempts": attempts}
            if late:
                rec.monitoring["late"] = True      # ran AFTER the cycle finished (explicit monitoring-only operation)
        except Exception as exc:  # noqa: BLE001 -- monitoring trouble never changes execution truth
            rec.monitoring = {"outcome": "FAILED", "status": "ERROR", "error": safe_error(exc),
                              "at": self._clock().isoformat(), "attempts": attempts}

    def monitor_cycle(self, cycle_id: str) -> CycleRecord:
        """EXPLICIT monitoring-only operation for an already-recorded cycle.

        Runs the monitoring service (read-only evaluation of persisted state) and records its real result on the cycle.
        It generates no signal, submits no order, touches no portfolio/ledger/intent, and creates no cycle. A cycle whose
        monitoring already COMPLETED is returned unchanged (no duplicate run). The result is flagged `late` because it
        evaluates state as of NOW, not as of the cycle.
        """
        if self._monitor_factory is None:
            raise PaperSetupError("no monitoring service is configured")
        store = self._open_store()
        with self._lock():
            rec = store.get_cycle(cycle_id)
            if rec is None:
                raise PaperSetupError(f"cycle {cycle_id} not found")
            if rec.experiment_id != self._config.experiment_id:
                raise PaperSetupError("that cycle belongs to a different experiment")
            if rec.status not in self.MONITORED_STATUSES:
                raise PaperSetupError(f"monitoring is not applicable to a cycle that ended {rec.status.value}")
            if (rec.monitoring or {}).get("outcome") == "COMPLETED" or (rec.monitoring or {}).get("run_id"):
                rec.replayed = True
                return rec
            rec.monitoring = {**(rec.monitoring or {}), "outcome": "PENDING", "status": "PENDING"}
            self._persist(store, rec)
            self._monitor(rec, late=True)
            self._persist(store, rec)
            return rec

    # ================================================================== human approval (Sprint 20, ADR-0056)
    def _build_intent(self, rec: CycleRecord, key: str, signal: Signal, side: str, quantity: float, approved_qty: float,
                      price: float, stop_price: float | None) -> IntentRecord:
        return IntentRecord(
            intent_id=_sha(key, "intent")[:32], cycle_id=rec.cycle_id, experiment_id=self._config.experiment_id,
            client_order_id="atp-paper-" + _sha(key, "order")[:24], symbol=self._config.symbol, decision_bar=rec.decision_bar,
            state=IntentState.INTENT_RECORDED, side=side, quantity=quantity, approved_quantity=approved_qty,
            direction=signal.direction.value, reference_price=price, stop_price=stop_price, signal=rec.signal,
            risk_decision=rec.risk_decision, execution_config=rec.provenance["execution"],
            created_at=self._clock().isoformat())

    @staticmethod
    def portfolio_fingerprint(portfolio) -> str:
        st = portfolio_state(portfolio)
        payload = {"cash": round(float(st.cash), 6), "positions": {k: [round(float(v[0]), 8), round(float(v[1]), 8)]
                                                                    for k, v in sorted(st.positions.items())}}
        return _sha(json.dumps(payload, sort_keys=True))

    def _propose(self, store: PaperStore, rec: CycleRecord, intent: IntentRecord, portfolio) -> CycleRecord:
        """Persist the risk-approved action as a PROPOSAL. No intent row, no ledger entry, no order, no portfolio change."""
        now = self._clock()
        proposal = ProposalRecord(
            proposal_id=_sha(rec.idempotency_key or rec.cycle_id, "proposal", now.isoformat())[:32], cycle_id=rec.cycle_id,
            experiment_id=self._config.experiment_id, status=ProposalStatus.PENDING, created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=self._ttl)).isoformat(), strategy_name=self._config.strategy_name,
            strategy_hash=rec.strategy_hash, config_hash=rec.config_hash, symbol=self._config.symbol,
            interval=self._config.interval, decision_bar=rec.decision_bar, side=intent.side, direction=intent.direction,
            quantity=intent.quantity, approved_quantity=intent.approved_quantity, reference_price=intent.reference_price,
            stop_price=intent.stop_price, signal=rec.signal, risk_decision=rec.risk_decision,
            execution_config=rec.provenance["execution"], dataset=rec.dataset,
            portfolio_fingerprint=self.portfolio_fingerprint(portfolio), intent_id=intent.intent_id,
            client_order_id=intent.client_order_id, config=_jsonable(asdict(self._config)))
        rec.status, rec.reason_code = CycleStatus.AWAITING_APPROVAL, "PROPOSAL_PENDING"
        rec.reason = (f"{intent.side} {intent.quantity:g} {intent.symbol} proposed at reference {intent.reference_price:g}; "
                      "no order exists until a human approves the proposal")
        rec.finished_at = now.isoformat()
        rec.monitoring = self._monitor_skipped(rec)
        if not store.add_proposal_and_cycle(proposal, rec):
            return self._finish(store, rec, CycleStatus.BLOCKED, "PROPOSAL_ALREADY_EXISTS", "a proposal with this id exists")
        return rec

    def _expire_due(self, store: PaperStore) -> None:
        now = self._clock()
        for p in store.list_proposals(self._config.experiment_id, (ProposalStatus.PENDING,)):
            if datetime.fromisoformat(p.expires_at) <= now:
                if store.set_proposal_status(p.proposal_id, ProposalStatus.EXPIRED, expect=(ProposalStatus.PENDING,),
                                             decided_at=now.isoformat(), reason="expired before approval") is not None:
                    cyc = store.get_cycle(p.cycle_id)
                    if cyc is not None:
                        self._finish(store, cyc, CycleStatus.PROPOSAL_EXPIRED, "PROPOSAL_EXPIRED",
                                     "the proposal expired unapproved; no order was submitted")

    def list_proposals(self, statuses: tuple[ProposalStatus, ...] | None = None, limit: int = 50) -> list[ProposalRecord]:
        return self._open_store().list_proposals(self._config.experiment_id, statuses, limit)

    def reject_proposal(self, proposal_id: str, reason: str = "rejected by user") -> CycleRecord:
        """Decline a pending proposal. Submits nothing. Repeating it is a no-op returning the same record."""
        store = self._open_store()
        with self._lock():
            prop = self._own_proposal(store, proposal_id)
            cyc = store.get_cycle(prop.cycle_id)
            if prop.status is ProposalStatus.REJECTED and cyc is not None:
                cyc.replayed = True
                return cyc
            self._expire_due(store)
            now = self._clock().isoformat()
            if store.set_proposal_status(proposal_id, ProposalStatus.REJECTED, expect=(ProposalStatus.PENDING,),
                                         decided_at=now, reason=reason) is None:
                cur = store.get_proposal(proposal_id)
                raise PaperProposalError("NOT_PENDING", f"the proposal is {cur.status.value if cur else 'missing'}; it cannot be rejected")
            return self._finish(store, cyc, CycleStatus.PROPOSAL_REJECTED, "PROPOSAL_REJECTED",
                                "the proposal was rejected by the user; no order was submitted")

    def _own_proposal(self, store: PaperStore, proposal_id: str) -> ProposalRecord:
        prop = store.get_proposal(proposal_id)
        if prop is None:
            raise PaperProposalError("NOT_FOUND", f"proposal {proposal_id} not found")
        if prop.experiment_id != self._config.experiment_id:
            raise PaperProposalError("WRONG_ACCOUNT", "that proposal belongs to a different account")
        return prop

    def _invalidate(self, store: PaperStore, prop: ProposalRecord, cyc: CycleRecord, code: str, reason: str):
        store.set_proposal_status(prop.proposal_id, ProposalStatus.INVALIDATED,
                                  expect=(ProposalStatus.PENDING,), decided_at=self._clock().isoformat(), reason=f"{code}: {reason}")
        self._finish(store, cyc, CycleStatus.PROPOSAL_INVALIDATED, code, reason)
        raise PaperProposalError(code, reason)

    def approve_proposal(self, proposal_id: str) -> CycleRecord:
        """Approve ONE exact persisted proposal. Every condition is re-checked authoritatively first; the order goes through
        the ordinary durable-intent -> PaperBroker path; approving twice yields the same single execution."""
        store = self._open_store()
        self._require_account(store)
        registry = self._registry()
        self._portfolio(registry)
        instance, shash = self.resolve_strategy()
        with self._lock():
            prop = self._own_proposal(store, proposal_id)
            cyc = store.get_cycle(prop.cycle_id)
            if cyc is None:
                raise PaperProposalError("NOT_FOUND", "the proposal's cycle record is missing")
            if prop.status is ProposalStatus.EXECUTED:
                if cyc.status in NON_TERMINAL_CYCLE:       # an uncertain submission: resolve it by lookup, never by resubmitting
                    self._recover_locked(store, registry)
                    cyc = store.get_cycle(prop.cycle_id) or cyc
                cyc.replayed = True
                return cyc
            if prop.status in (ProposalStatus.REJECTED, ProposalStatus.EXPIRED, ProposalStatus.INVALIDATED):
                raise PaperProposalError(prop.status.value, f"the proposal is {prop.status.value} and cannot be approved; "
                                                           "run a fresh cycle to evaluate current conditions")
            if prop.status is ProposalStatus.PENDING:
                if datetime.fromisoformat(prop.expires_at) <= self._clock():
                    self._expire_due(store)
                    raise PaperProposalError("EXPIRED", "the proposal expired; run a fresh cycle to evaluate current conditions")
                self._revalidate(store, registry, prop, cyc, shash)
            now = self._clock().isoformat()
            if store.set_proposal_status(proposal_id, ProposalStatus.APPROVING, expect=(ProposalStatus.PENDING, ProposalStatus.APPROVING),
                                         decided_at=now, reason="approved by user") is None:
                raise PaperProposalError("NOT_PENDING", "the proposal changed state while approving; nothing was submitted")
            intent = IntentRecord(
                intent_id=prop.intent_id, cycle_id=prop.cycle_id, experiment_id=prop.experiment_id,
                client_order_id=prop.client_order_id, symbol=prop.symbol, decision_bar=prop.decision_bar,
                state=IntentState.INTENT_RECORDED, side=prop.side, quantity=prop.quantity, approved_quantity=prop.approved_quantity,
                direction=prop.direction, reference_price=prop.reference_price, stop_price=prop.stop_price, signal=prop.signal,
                risk_decision=prop.risk_decision, execution_config=prop.execution_config, created_at=now)
            cyc.intent_id, cyc.client_order_id = intent.intent_id, intent.client_order_id
            cyc.status, cyc.reason_code, cyc.reason = CycleStatus.AWAITING_EXECUTION, "APPROVED", "approved by the user; executing"
            if store.record_intent(intent, cyc):
                self._faults.fire("after_intent_persisted")
                result = self._execute_and_conclude(store, registry, intent, cyc)
            else:                                         # the intent already exists (repeat approval / crash recovery)
                self._recover_locked(store, registry)
                result = store.get_cycle(prop.cycle_id) or cyc
            store.set_proposal_status(proposal_id, ProposalStatus.EXECUTED, expect=(ProposalStatus.APPROVING,),
                                      decided_at=self._clock().isoformat(), reason=f"cycle ended {result.status.value}")
            return result

    def _revalidate(self, store: PaperStore, registry, prop: ProposalRecord, cyc: CycleRecord, shash: str) -> None:
        cfg = self._config
        if prop.strategy_hash != shash or prop.config_hash != cfg.identity_hash() or prop.symbol != cfg.symbol \
                or prop.interval != cfg.interval:
            self._invalidate(store, prop, cyc, "CONFIG_CHANGED", "the strategy, configuration or instrument changed since the proposal")
        hard = [c for c in self.preflight(check_data=False).checks if c.status is CheckStatus.FAIL]
        if hard:
            raise PaperProposalError("PREFLIGHT_FAILED", "safety checks failed: " + "; ".join(f"{c.name}: {c.detail}" for c in hard)
                                     + " (the proposal stays pending; nothing was submitted)")
        try:
            ds = self._fetch_dataset()                    # always fresh
        except (DataProviderError, DataValidationError) as exc:
            raise PaperProposalError("DATA_UNAVAILABLE", f"market data could not be validated now: {safe_error(exc)} "
                                                         "(the proposal stays pending; nothing was submitted)")
        complete, _ = bars.split_complete(ds.candles, self._cal, self._clock())
        if complete.empty or pd.Timestamp(complete.index[-1]).isoformat() != prop.decision_bar:
            self._invalidate(store, prop, cyc, "DATA_CHANGED", "the latest complete decision bar is no longer the one the proposal was based on")
        latest_close = float(complete.iloc[-1]["close"])
        if not math.isclose(latest_close, prop.reference_price, rel_tol=1e-6, abs_tol=1e-6):
            self._invalidate(store, prop, cyc, "DATA_CHANGED", "the decision-bar price differs from the proposal's reference price")
        portfolio = self._portfolio(registry)
        if self.portfolio_fingerprint(portfolio) != prop.portfolio_fingerprint:
            self._invalidate(store, prop, cyc, "PORTFOLIO_CHANGED", "cash or positions changed since the proposal")
        prior = store.intent_for_bar(cfg.experiment_id, prop.symbol, prop.decision_bar)
        if prior is not None and prior.client_order_id != prop.client_order_id:
            self._invalidate(store, prop, cyc, "DUPLICATE_ORDER", "an order for this decision bar already exists")
        signal = signal_from_record(prop.signal)
        try:
            if signal.direction is SignalDirection.FLAT:
                decision = self._risk.decide_close(signal, portfolio)
            else:
                decision = self._risk.decide(signal, portfolio, entry_price=prop.reference_price, stop_price=prop.stop_price)
            ok = bool(decision.approved) and float(decision.final_approved_quantity) + 1e-9 >= prop.quantity
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            self._invalidate(store, prop, cyc, "RISK_CHANGED", "the risk controls no longer approve this proposal")

    # ================================================================== recovery
    def _recover_locked(self, store: PaperStore, registry) -> list[CycleRecord]:
        """Resolve unfinished intents from authoritative recorded state, never by blind resubmission."""
        out: list[CycleRecord] = []
        exp = self._config.experiment_id
        for intent in store.list_intents(exp, unresolved_only=True):
            cycle = store.get_cycle(intent.cycle_id) or self._new_record(None)
            try:
                concluded = self._execute_and_conclude(store, registry, intent, cycle)
            except (PaperSetupError, PaperLockedError):
                raise
            except Exception as exc:  # noqa: BLE001
                concluded = self._finish_failure(store, cycle, exc)
            out.append(concluded)
        # cycles whose intent already reached a terminal state but whose own record was never finalised
        for cycle in store.list_cycles(exp, limit=200, statuses=tuple(NON_TERMINAL_CYCLE)):
            if cycle.intent_id is None:
                continue
            intent = store.get_intent(cycle.intent_id)
            if intent is None or intent.state is IntentState.INTENT_RECORDED or intent.state is IntentState.FILLED_AT_BROKER:
                continue
            fill = store.get_fill(intent.client_order_id)
            cycle.fill = fill.to_dict() if fill else cycle.fill
            status = CycleStatus.ORDER_FILLED if intent.state is IntentState.APPLIED else CycleStatus.ORDER_REJECTED
            self._finish(store, cycle, status, "RECOVERED", "finalised by recovery from recorded intent state")
            out.append(cycle)
        # a proposal whose approval crashed mid-flight stays APPROVING until its order is resolved; once the cycle it
        # produced is terminal the proposal is EXECUTED. Without this it would block every later cycle.
        for prop in store.list_proposals(exp, (ProposalStatus.APPROVING,)):
            cyc = store.get_cycle(prop.cycle_id)
            if cyc is not None and cyc.intent_id and cyc.status not in NON_TERMINAL_CYCLE:
                store.set_proposal_status(prop.proposal_id, ProposalStatus.EXECUTED, expect=(ProposalStatus.APPROVING,),
                                          decided_at=self._clock().isoformat(), reason=f"cycle ended {cyc.status.value} (recovered)")
        return out
