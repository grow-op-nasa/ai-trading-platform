"""Paper-trading operations for the UI (Sprint 19).

Every mutating call is ONE call into the Sprint 18 runtime (`PaperTradingRuntime`) -- the same path the CLI uses. This
module adds no execution logic, no risk logic, no order creation, and cannot reach a live broker (the runtime is
hard-bound to PaperBroker). Idempotency stays in the backend (cycle key, intent and ledger UNIQUE constraints); the
`request_token` guard below only stops one UI submission from being executed twice by a Streamlit rerun.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.appsvc.paths import AppPaths
from src.appsvc.strategies import eligible_names
from src.paper.config import PaperConfigError, PaperRunConfig
from src.paper.models import CheckStatus, CycleRecord, CycleStatus, PreflightReport
from src.paper.runtime import PaperLockedError, PaperSetupError, PaperTradingRuntime
from src.paper.store import PaperStore, PaperStoreError

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

DEFAULT_FORM = {
    "interval": "1d", "lookback_days": 400,
    "risk": {"allocation_per_trade_pct": 0.10, "max_portfolio_exposure_pct": 0.50, "risk_pct_per_trade": 0.01},
    "stop": {"type": "atr", "period": 14, "multiple": 2.0},
    "execution": {"timing": "SIGNAL_BAR_CLOSE", "slippage_bps": 5.0, "fee_bps": 5.0, "fixed_fee": 0.0},
}

OUTCOME_MEANING = {
    "NO_ACTION": "A valid outcome: the strategy gave no actionable signal (or the position already matched). No order was created.",
    "NO_NEW_BAR": "No new completed bar to decide on yet. Nothing was recorded or changed.",
    "RISK_REJECTED": "The risk engine declined the proposed trade. No order was created.",
    "ORDER_FILLED": "A SIMULATED paper fill was recorded by the paper broker and applied to the paper portfolio. It is not a real-market fill and not evidence of profit.",
    "AWAITING_EXECUTION": "An execution intent is recorded but not yet resolved. Run again (or `paper status`) to recover; it will never be blindly resubmitted.",
    "ORDER_REJECTED": "The paper broker rejected the order (for example insufficient cash). Nothing was filled.",
    "BLOCKED": "Blocked by data or safety preconditions. No order was created.",
    "RECONCILIATION_REQUIRED": "The paper broker ledger and the portfolio disagree. No new risk is taken until a human resolves it.",
    "FAILED": "The cycle failed inside the service. Inspect the cycle record; durable state may exist.",
    "STARTED": "The cycle record was started but did not finish.",
}


@dataclass
class ProfileInfo:
    name: str
    path: Path
    config: PaperRunConfig | None
    error: str | None = None


@dataclass
class RunOutcome:
    record: CycleRecord | None = None
    refused: str | None = None              # preflight FAIL / setup problem: nothing was run
    refused_checks: list[dict] = field(default_factory=list)
    error: str | None = None                # an exception escaped the service; durable state may exist
    locked: bool = False
    reused_token: bool = False

    @property
    def ran(self) -> bool:
        return self.record is not None


class PaperService:
    def __init__(self, paths: AppPaths | None = None, *, market_data: Any = None,
                 monitor_factory_builder: Callable[[PaperRunConfig], Callable[[], Any]] | None = None,
                 runtime_kwargs: dict | None = None) -> None:
        self.paths = paths or AppPaths()
        self._market_data = market_data
        self._monitor_builder = monitor_factory_builder
        self._runtime_kwargs = dict(runtime_kwargs or {})
        self._tokens: dict[str, RunOutcome] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ profiles (config files = source of truth)
    def list_profiles(self) -> list[ProfileInfo]:
        d = self.paths.profiles_dir
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.glob("*.json")):
            try:
                out.append(ProfileInfo(p.stem, p, PaperRunConfig.from_file(p)))
            except PaperConfigError as exc:
                out.append(ProfileInfo(p.stem, p, None, str(exc)[:240]))
        return out

    def build_config(self, *, experiment_id: int, strategy_name: str, symbol: str, strategy_params: dict | None = None,
                     form: dict | None = None) -> PaperRunConfig:
        """Validate a configuration with the runtime's own validator. Only registered strategies are accepted."""
        if strategy_name not in eligible_names(self.paths):
            raise PaperConfigError(f"{strategy_name!r} is not a registered production strategy")
        f = form or DEFAULT_FORM
        raw = {"execution_mode": "paper", "experiment_id": experiment_id, "strategy_name": strategy_name, "symbol": symbol.strip().upper(),
               "strategy_params": strategy_params or {}, "interval": f["interval"], "lookback_days": f["lookback_days"],
               "risk": f["risk"], "stop": f["stop"], "execution": f["execution"]}
        return PaperRunConfig.from_dict(raw)

    def save_profile(self, name: str, cfg: PaperRunConfig) -> Path:
        """Explicit operator action: write a NEW profile file. Never overwrites."""
        if not _NAME.match(name or ""):
            raise PaperConfigError("profile name must be 1-64 letters, digits, '_', '-' or '.'")
        cfg.validate()
        self.paths.profiles_dir.mkdir(parents=True, exist_ok=True)
        target = self.paths.profiles_dir / f"{name}.json"
        if target.exists():
            raise PaperConfigError(f"profile {name!r} already exists; choose another name (existing files are never overwritten)")
        raw = {"execution_mode": cfg.execution_mode, "experiment_id": cfg.experiment_id, "strategy_name": cfg.strategy_name,
               "symbol": cfg.symbol, "strategy_params": cfg.strategy_params, "interval": cfg.interval,
               "lookback_days": cfg.lookback_days, "risk": cfg.risk, "stop": cfg.stop, "execution": cfg.execution}
        with open(target, "x") as fh:
            json.dump(raw, fh, indent=2)
        return target

    # ------------------------------------------------------------------ runtime construction
    def _md(self):
        if self._market_data is None:
            from src.data.service import MarketDataService
            self._market_data = MarketDataService()
        return self._market_data

    def _monitor_factory(self, cfg: PaperRunConfig):
        if self._monitor_builder is not None:
            return self._monitor_builder(cfg)

        def factory():
            from src.data.service import MarketDataService
            from src.experiments.registry import ExperimentRegistry
            from src.monitoring import MonitoringService, MonitoringStore, RegistryStateSource
            from src.monitoring.alerts.config import AlertConfig
            from src.monitoring.model_lookup import registry_model_lookup
            source = RegistryStateSource(ExperimentRegistry(self.paths.registry), experiment_id=cfg.experiment_id,
                                         model_lookup=registry_model_lookup())
            return MonitoringService(source, MonitoringStore(self.paths.monitoring_db), market_data=MarketDataService(),
                                     alerts=AlertConfig.from_env().event_settings())
        return factory

    def runtime(self, cfg: PaperRunConfig, *, monitoring: bool = True) -> PaperTradingRuntime:
        return PaperTradingRuntime(cfg, registry_path=self.paths.registry, store_path=self.paths.paper_db,
                                   market_data=self._md(), monitor_factory=self._monitor_factory(cfg) if monitoring else None,
                                   **self._runtime_kwargs)

    # ------------------------------------------------------------------ operations
    def preflight(self, cfg: PaperRunConfig, *, check_data: bool = True) -> PreflightReport:
        """Read-only. check_data=True makes one market-data request (an explicit user action in the UI)."""
        return self.runtime(cfg, monitoring=True).preflight(check_data=check_data)

    def initialize_account(self, cfg: PaperRunConfig, label: str) -> dict:
        """Explicit: record the paper broker baseline from the experiment's CURRENT saved portfolio (`paper init`)."""
        label = (label or "").strip()
        if not label:
            raise PaperSetupError("a label is required to initialise a paper account")
        return self.runtime(cfg, monitoring=False).initialize_account(label)

    def run_cycle(self, cfg: PaperRunConfig, *, request_token: str | None = None, monitoring: bool = True) -> RunOutcome:
        """Run exactly ONE bounded paper cycle through the authoritative runtime.

        The same hard-FAIL preflight gate as `paper run --once` applies (no data request). A repeated `request_token`
        returns the earlier outcome without calling the runtime again.
        """
        with self._lock:
            if request_token is not None and request_token in self._tokens:
                prior = self._tokens[request_token]
                return RunOutcome(prior.record, prior.refused, prior.refused_checks, prior.error, prior.locked, True)
            outcome = self._run(cfg, monitoring)
            if request_token is not None:
                self._tokens[request_token] = outcome
            return outcome

    def _run(self, cfg: PaperRunConfig, monitoring: bool) -> RunOutcome:
        try:
            rt = self.runtime(cfg, monitoring=monitoring)
            hard = [c for c in rt.preflight(check_data=False).checks if c.status is CheckStatus.FAIL]
            if hard:
                return RunOutcome(refused="preflight failed; nothing was run", refused_checks=[c.to_dict() for c in hard])
            return RunOutcome(record=rt.run_cycle())
        except (PaperSetupError, PaperStoreError) as exc:
            return RunOutcome(refused=str(exc)[:300])
        except PaperLockedError as exc:
            return RunOutcome(refused=str(exc)[:300], locked=True)
        except Exception as exc:  # noqa: BLE001 -- surfaced, never reported as success; the cycle record may exist
            return RunOutcome(error=f"{type(exc).__name__}: {str(exc)[:300]}")

    def reconcile(self, cfg: PaperRunConfig):
        """Explicit, observation-only: re-run reconciliation and record it. Never corrects anything."""
        return self.runtime(cfg, monitoring=False).reconcile_now()

    # ------------------------------------------------------------------ read-only inspection
    def store(self) -> PaperStore | None:
        if not self.paths.paper_exists():
            return None
        try:
            return PaperStore.open_existing(self.paths.paper_db)
        except PaperStoreError:
            return None

    def inspect(self, experiment_id: int, limit: int = 20) -> dict[str, Any]:
        s = self.store()
        if s is None:
            return {"available": False, "reason": f"no paper database at {self.paths.paper_db}"}
        return {"available": True, "account": s.get_account(experiment_id), "cycles": s.list_cycles(experiment_id, limit),
                "intents": s.list_intents(experiment_id, limit=limit), "unresolved": s.list_intents(experiment_id, unresolved_only=True),
                "fills": s.list_fills(experiment_id)[-limit:], "reconciliations": s.list_reconciliations(experiment_id, limit),
                "latest_reconciliation": s.latest_reconciliation(experiment_id)}

    def trace(self, cycle_id: str) -> dict[str, Any]:
        """Connect a cycle to its stored related records; state what is not linked rather than guessing."""
        s = self.store()
        if s is None:
            return {"available": False}
        c = s.get_cycle(cycle_id)
        if c is None:
            return {"available": False, "reason": "cycle not found"}
        limits: list[str] = []
        intent = s.get_intent(c.intent_id) if c.intent_id else None
        fill = s.get_fill(c.client_order_id) if c.client_order_id else None
        if not c.intent_id:
            limits.append("No execution intent was created for this cycle (no order was proposed).")
        recon = next((r for r in s.list_reconciliations(c.experiment_id, 200) if r.recon_id == c.reconciliation_id), None) \
            if c.reconciliation_id else None
        run = findings = events = None
        mon = c.monitoring or {}
        rid = mon.get("run_id")
        if rid and self.paths.monitoring_exists():
            from src.monitoring.store import MonitoringStore
            ms = MonitoringStore(self.paths.monitoring_db)
            run = ms.get_run(rid)
            if run is not None:
                findings = [f for f in (ms.get_finding(i) for i in run.finding_ids) if f is not None]
                events = ms.alerts.events_for_run(rid)
            else:
                limits.append("The monitoring run id is recorded but the run is not in the monitoring database.")
        elif rid:
            limits.append("The monitoring run id is recorded but no monitoring database exists to look it up in.")
        else:
            limits.append("No monitoring run is linked to this cycle (monitoring was skipped, failed, or not yet run).")
        return {"available": True, "cycle": c, "intent": intent, "fill": fill, "reconciliation": recon,
                "monitoring_run": run, "findings": findings, "alert_events": events, "limitations": limits}
