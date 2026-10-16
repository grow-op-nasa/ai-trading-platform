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
import re
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
    ReconStatus,
)
from src.paper.reconcile import portfolio_state, reconcile, states_match
from src.paper.store import DEFAULT_DB_PATH, PaperStore, PaperStoreError
from src.risk.portfolio_risk import PortfolioRiskEngine
from src.signals.models import Signal, SignalDirection
from src.strategies.identity import strategy_version
from src.utils.hashing import dataframe_fingerprint

_NO_RETRY = (CycleStatus.NO_ACTION, CycleStatus.RISK_REJECTED, CycleStatus.ORDER_REJECTED,
             CycleStatus.ORDER_FILLED, CycleStatus.RECONCILIATION_REQUIRED)
_SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_\-]+|(?i:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+)")


class PaperSetupError(RuntimeError):
    """The target cannot be run at all (missing registry/account/strategy); nothing was recorded."""


class PaperLockedError(RuntimeError):
    """Another paper run holds the lock for this account."""


def safe_error(exc: BaseException, limit: int = 300) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return _SECRET.sub("[redacted]", text)[:limit]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def _sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def signal_to_record(signal: Signal) -> dict[str, Any]:
    return {"id": str(signal.id), "timestamp": pd.Timestamp(signal.timestamp).isoformat(), "symbol": signal.symbol,
            "direction": signal.direction.value, "confidence": signal.confidence,
            "metadata": _jsonable(signal.metadata)}


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
    ) -> None:
        config.validate()
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
        """(instance, strategy_hash) via the ordinary production registry only."""
        import src.strategies  # noqa: F401  registers built-in + promoted strategies (the production path)
        from src.strategies.registry import get_strategy_class

        name = self._config.strategy_name
        try:
            cls = get_strategy_class(name)
        except KeyError as exc:
            raise PaperSetupError(f"strategy {name!r} is not registered in the production registry") from exc
        module = getattr(cls, "__module__", "")
        if not any(module == p or module.startswith(p + ".") for p in self._prefixes):
            raise PaperSetupError(f"strategy {name!r} comes from {module!r}, which is not an approved production "
                                  "strategy module")
        try:
            instance = cls(symbol=self._config.symbol, **self._config.strategy_params)
            shash = strategy_version(cls)
        except Exception as exc:  # noqa: BLE001
            raise PaperSetupError(f"strategy {name!r} could not be constructed: {safe_error(exc)}") from exc
        return instance, shash

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
            _, shash = self.resolve_strategy()
            add("strategy", CheckStatus.PASS, f"{self._config.strategy_name} resolved via production registry; hash {shash[:16]}")
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
                    lock.acquire(); lock.release()
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
            ds = self._fetch_dataset()
        except Exception as exc:  # noqa: BLE001
            return ("market_data", CheckStatus.FAIL, safe_error(exc))
        return ("market_data", CheckStatus.PASS, f"{len(ds.candles)} bars via {ds.provider}, latest {ds.dataset_end}")

    def _fetch_dataset(self):
        end = self._clock().date()
        start = end - timedelta(days=self._config.lookback_days)
        return self._md.get_dataset(self._config.symbol, start=start, end=end,
                                    interval=self._config.interval_enum(), use_cache=False)

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
                ds = self._fetch_dataset()
            except (DataProviderError, DataValidationError) as exc:
                code = "DATA_INVALID" if isinstance(exc, DataValidationError) else "DATA_UNAVAILABLE"
                return self._finish(store, rec, CycleStatus.BLOCKED, code, safe_error(exc))
            complete, dropped = bars.split_complete(ds.candles, self._cal, self._clock())
            if complete.empty:
                return self._finish(store, rec, CycleStatus.BLOCKED, "NO_COMPLETE_BAR",
                                    "the dataset contains no complete daily bar")
            try:
                expected = bars.expected_completed_session(self._cal, self._clock())
            except ValueError as exc:
                return self._finish(store, rec, CycleStatus.BLOCKED, "CALENDAR_UNSUPPORTED", safe_error(exc))
            decision_bar = pd.Timestamp(complete.index[-1])
            latest_session = bars.bar_session(decision_bar)
            if latest_session < expected:
                if bars.past_grace(self._cal, expected, self._clock()):
                    return self._finish(store, rec, CycleStatus.BLOCKED, "DATA_STALE",
                                        f"latest complete bar is {latest_session}, expected {expected}")
                rec.status, rec.reason_code = CycleStatus.NO_NEW_BAR, "AWAITING_PROVIDER_BAR"
                rec.reason = f"session {expected} closed but its bar is not yet published (within grace)"
                rec.finished_at = self._clock().isoformat()
                return rec                                               # not persisted: nothing happened
            rec.decision_bar = decision_bar.isoformat()
            rec.dataset = {
                "provider": ds.provider, "dataset_hash": ds.content_hash,
                "complete_bars_hash": dataframe_fingerprint(complete), "bars_used": int(len(complete)),
                "dataset_start": ds.dataset_start.isoformat(), "dataset_end": ds.dataset_end.isoformat(),
                "dropped_incomplete_bars": [pd.Timestamp(t).isoformat() for t in dropped],
                "validation_clean": ds.validation_report.is_clean, "gap_count": ds.validation_report.gaps.count,
                "session_policy": ds.session_policy.value, "session_timezone": ds.session_timezone,
                "cache_used": False}

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

            # ---- strategy (complete bars only, nothing after the decision bar)
            try:
                prepared = instance.prepare(complete.copy())
                raw = instance.generate_signals(prepared)
                if not isinstance(raw, list) or not all(isinstance(s, Signal) for s in raw):
                    raise ValueError("generate_signals() must return list[Signal]")
            except Exception as exc:  # noqa: BLE001
                return self._finish(store, rec, CycleStatus.FAILED, "STRATEGY_ERROR", safe_error(exc))
            at_bar = [s for s in raw if pd.Timestamp(s.timestamp) == decision_bar]
            if any(s.symbol != cfg.symbol for s in at_bar):
                return self._finish(store, rec, CycleStatus.FAILED, "SIGNAL_SYMBOL_MISMATCH",
                                    "strategy emitted a signal for a different instrument")
            if not at_bar:
                return self._finish(store, rec, CycleStatus.NO_ACTION, "NO_SIGNAL",
                                    "the strategy produced no signal at the decision bar")
            if len(at_bar) > 1:
                return self._finish(store, rec, CycleStatus.BLOCKED, "AMBIGUOUS_SIGNALS",
                                    f"{len(at_bar)} signals at one decision bar; refusing to choose")
            signal = at_bar[0]
            rec.signal = signal_to_record(signal)

            # ---- risk
            price = float(complete.loc[decision_bar, "close"])
            portfolio = self._portfolio(registry)
            stop_price: float | None = None
            try:
                if signal.direction is SignalDirection.FLAT:
                    decision = self._risk.decide_close(signal, portfolio)
                else:
                    history = complete.loc[:decision_bar]
                    stop = self._stop.stop_price(signal, history, entry_price=price)
                    if not stop.available:
                        rec.risk_decision = {"approved": False, "stop_unavailable": stop.reason}
                        return self._finish(store, rec, CycleStatus.RISK_REJECTED, "STOP_UNAVAILABLE", str(stop.reason))
                    stop_price = float(stop.stop_price)
                    decision = self._risk.decide(signal, portfolio, entry_price=price, stop_price=stop_price)
            except Exception as exc:  # noqa: BLE001
                return self._finish(store, rec, CycleStatus.FAILED, "RISK_ENGINE_ERROR", safe_error(exc))
            rec.risk_decision = _jsonable(asdict(decision))
            if signal.direction is SignalDirection.FLAT and not decision.approved and \
                    getattr(decision.rejection_reason, "value", None) == "NO_POSITION_TO_CLOSE":
                return self._finish(store, rec, CycleStatus.NO_ACTION, "ALREADY_FLAT",
                                    "desired state FLAT is already satisfied")
            if not decision.approved:
                return self._finish(store, rec, CycleStatus.RISK_REJECTED,
                                    str(getattr(decision.rejection_reason, "value", decision.rejection_reason)),
                                    decision.explanation)
            if getattr(decision.capital_model, "value", None) == "NOT_MODELED":
                rec.risk_decision["runtime_policy_block"] = "SHORT_EXPOSURE_NOT_MODELED"
                return self._finish(store, rec, CycleStatus.RISK_REJECTED, "SHORT_NOT_MODELED",
                                    "short exposure has no modelled capital/margin/borrow; the paper runtime fails closed")

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
            intent = IntentRecord(
                intent_id=_sha(key, "intent")[:32], cycle_id=rec.cycle_id, experiment_id=cfg.experiment_id,
                client_order_id="atp-paper-" + _sha(key, "order")[:24], symbol=cfg.symbol, decision_bar=rec.decision_bar,
                state=IntentState.INTENT_RECORDED, side=side.value, quantity=quantity,
                approved_quantity=approved_qty,
                direction=signal.direction.value, reference_price=price, stop_price=stop_price,
                signal=rec.signal, risk_decision=rec.risk_decision, execution_config=rec.provenance["execution"],
                created_at=self._clock().isoformat())
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
        return out
