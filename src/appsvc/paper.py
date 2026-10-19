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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.snapshots import SnapshotStore
from src.appsvc.paths import AppPaths
from src.appsvc.strategies import eligible_names
from src.paper.config import PaperConfigError, PaperRunConfig
from src.paper.models import CheckStatus, CycleRecord, PreflightReport, ProposalRecord, ProposalStatus
from src.paper.runtime import PaperLockedError, PaperProposalError, PaperSetupError, PaperTradingRuntime
from src.paper.store import PaperStore, PaperStoreError

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

DEFAULT_FORM = {
    "interval": "1d", "lookback_days": 400,
    "risk": {"allocation_per_trade_pct": 0.10, "max_portfolio_exposure_pct": 0.50, "risk_pct_per_trade": 0.01},
    "stop": {"type": "atr", "period": 14, "multiple": 2.0},
    "execution": {"timing": "SIGNAL_BAR_CLOSE", "slippage_bps": 5.0, "fee_bps": 5.0, "fixed_fee": 0.0},
}

# Sprint 20 policy for the integrated Run: a fresh preflight (incl. one market-data request) must be clean for the checks that
# protect capital. FAIL always blocks. BLOCKED blocks too, except `unresolved_intents`/`reconciliation`, which the runtime's own
# gates handle deterministically (recovery, then a persisted RECONCILIATION_REQUIRED cycle). NOT_EVALUABLE blocks only for the
# safety-critical checks below; for monitoring/alerting/duplicate-run it is a visible warning, never a pass.
RUNTIME_HANDLED = frozenset({"unresolved_intents", "reconciliation"})
SAFETY_CRITICAL = frozenset({"configuration", "paper_only_execution", "paper_broker", "registry", "portfolio", "strategy",
                             "instrument_and_timeframe", "risk_and_execution_settings", "paper_database", "paper_account",
                             "persistence_writable", "market_data", "reconciliation"})


def blocking_checks(report: PreflightReport) -> list:
    out = []
    for c in report.checks:
        if c.status is CheckStatus.PASS:
            continue
        if c.status is CheckStatus.FAIL:
            out.append(c)
        elif c.status is CheckStatus.BLOCKED and c.name not in RUNTIME_HANDLED:
            out.append(c)
        elif c.status is CheckStatus.NOT_EVALUABLE and c.name in SAFETY_CRITICAL:
            out.append(c)
    return out


def warning_checks(report: PreflightReport) -> list:
    block = {c.name for c in blocking_checks(report)}
    return [c for c in report.checks if c.status is not CheckStatus.PASS and c.name not in block]


def stages_from_record(rec: CycleRecord, *, preflight_ran: bool = True) -> list[dict]:
    """Progress/audit stages derived ONLY from what the persisted cycle record proves (never from the UI's expectations)."""
    st = rec.status.value
    out: list[dict] = []
    add = lambda n, s, d="": out.append({"stage": n, "status": s, "detail": d})  # noqa: E731
    if preflight_ran:
        add("Preconditions and fresh preflight", "DONE", "passed before the cycle started")
    add("Market data retrieved and validated", "DONE" if rec.dataset else ("BLOCKED" if st == "BLOCKED" else "NOT_REACHED"),
        f"{(rec.dataset or {}).get('provider', '')} bars={ (rec.dataset or {}).get('bars_used', '') } decision bar {rec.decision_bar or ''}".strip()
        if rec.dataset else (rec.reason or ""))
    evaluated = bool(rec.decision_bar and rec.dataset) and not (st == "FAILED" and rec.reason_code in ("STRATEGY_ERROR", "SIGNAL_SYMBOL_MISMATCH")) \
        and not (st == "BLOCKED" and rec.reason_code == "AMBIGUOUS_SIGNALS")
    add("Strategy evaluated", "DONE" if evaluated else ("FAILED" if st == "FAILED" else "NOT_REACHED"),
        "signal recorded" if rec.signal else ("no signal at the decision bar" if st == "NO_ACTION" and rec.reason_code == "NO_SIGNAL" else rec.reason or ""))
    add("Risk controls applied", "DONE" if rec.risk_decision else "NOT_REACHED",
        ("approved" if (rec.risk_decision or {}).get("approved") else "rejected") if rec.risk_decision else "")
    if st == "AWAITING_APPROVAL":
        add("Proposal created", "DONE", "persisted; awaiting your decision (no order exists)")
        add("Order submitted", "NOT_REACHED", "requires explicit approval")
    elif st in ("PROPOSAL_REJECTED", "PROPOSAL_EXPIRED", "PROPOSAL_INVALIDATED"):
        add("Proposal created", "DONE", "")
        add("Order submitted", "NOT_REACHED", st.replace("PROPOSAL_", "proposal ").lower() + "; no order")
    elif rec.client_order_id:
        add("Proposal approved", "DONE", "")
        add("Order submitted and recorded", "DONE" if rec.fill else "UNCERTAIN", "fill recorded" if rec.fill else "outcome not yet conclusive")
        add("Reconciled", rec.reconciliation_status or "NOT_REACHED", "")
    mon = (rec.monitoring or {}).get("outcome")
    add("Post-cycle monitoring", {"COMPLETED": "DONE", "FAILED": "FAILED", "PENDING": "PENDING"}.get(mon, "SKIPPED" if mon else "NOT_RECORDED"),
        (rec.monitoring or {}).get("reason") or (rec.monitoring or {}).get("error") or (f"run {(rec.monitoring or {}).get('run_id')}" if mon == "COMPLETED" else ""))
    return out


OUTCOME_MEANING = {
    "AWAITING_APPROVAL": "A proposed paper order is awaiting your decision. No order has been submitted.",
    "PROPOSAL_REJECTED": "You rejected the proposal. No order was submitted.",
    "PROPOSAL_EXPIRED": "The proposal expired before approval and cannot be approved. No order was submitted. Run a fresh cycle.",
    "PROPOSAL_INVALIDATED": "Conditions changed after the proposal, so it cannot be approved. No order was submitted. Run a fresh cycle.",
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
    preflight: dict | None = None             # the fresh preflight that gated this run (Sprint 20)
    proposal_error: str | None = None         # machine code of a refused proposal decision (nothing was submitted)

    @property
    def ran(self) -> bool:
        return self.record is not None


def monitoring_state(mon: dict | None, *, run_found: bool = False, run_missing: str | None = None) -> dict[str, Any]:
    """Interpret a cycle's persisted `monitoring` field into ONE explicit state for the UI (never a generic 'not recorded')."""
    if mon is None:
        return {"state": "NOT_RECORDED", "run_id": None, "can_run": True,
                "message": "No monitoring outcome was recorded for this cycle. It was created before post-cycle monitoring covered "
                           "cycles like this one (or monitoring never ran); no monitoring run is linked, and none is inferred from timestamps."}
    outcome = mon.get("outcome")
    if outcome is None:                       # legacy shape written before the explicit outcome existed
        outcome = "COMPLETED" if mon.get("run_id") else ("FAILED" if mon.get("status") == "ERROR" else mon.get("status") or "UNKNOWN")
    rid = mon.get("run_id")
    if outcome == "COMPLETED" and rid:
        if run_found:
            return {"state": "COMPLETED", "run_id": rid, "can_run": False, "message": f"Monitoring completed: run {rid} is linked."}
        return {"state": "LINKED_RUN_MISSING", "run_id": rid, "can_run": False, "message": run_missing or "The linked run cannot be loaded."}
    if outcome == "FAILED":
        return {"state": "FAILED", "run_id": None, "can_run": True,
                "message": f"Post-cycle monitoring FAILED (the cycle's execution outcome is unaffected): {mon.get('error', 'unknown error')}"}
    if outcome == "PENDING":
        return {"state": "PENDING", "run_id": None, "can_run": True,
                "message": "Monitoring is pending: it was started but no result was recorded (it may still be running, or the process stopped)."}
    if outcome == "SKIPPED":
        return {"state": "SKIPPED", "run_id": None, "can_run": False, "message": "Monitoring was skipped: " + str(mon.get("reason", "by policy"))}
    if outcome == "NOT_CONFIGURED":
        return {"state": "NOT_CONFIGURED", "run_id": None, "can_run": True,
                "message": "Monitoring was not configured for this run: " + str(mon.get("reason", "no monitoring service"))}
    return {"state": "UNKNOWN", "run_id": rid, "can_run": False, "message": f"Unrecognised monitoring state {outcome!r}; shown as stored."}


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
                                   **{"snapshots": SnapshotStore(self.paths.snapshots_dir), **self._runtime_kwargs})

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

    def create_paper_account(self, *, strategy_name: str, symbol: str, starting_cash: float, label: str) -> int:
        """EXPLICIT: create a NEW, empty paper account (an experiment record plus a cash-only portfolio) with the existing registry
        APIs, for a user who has no portfolio to select. It never touches an existing experiment and is not a backtest: the
        record's notes say so. Paper initialisation (the broker baseline) remains a separate explicit step."""
        if strategy_name not in eligible_names(self.paths):
            raise PaperConfigError(f"{strategy_name!r} is not a registered production strategy")
        if not (symbol or "").strip():
            raise PaperConfigError("an instrument symbol is required")
        if isinstance(starting_cash, bool) or not isinstance(starting_cash, (int, float)) or not 100 <= starting_cash <= 1_000_000_000:
            raise PaperConfigError("starting cash must be a number between 100 and 1,000,000,000")
        if not (label or "").strip():
            raise PaperConfigError("a label is required")
        from src.experiments.registry import ExperimentRegistry
        from src.portfolio.models import Portfolio
        reg = ExperimentRegistry(self.paths.registry)
        exp = reg.log_experiment(strategy_name=strategy_name, changed={}, metrics_before={}, metrics_after={}, decision="INCONCLUSIVE",
                                 notes=f"PAPER ACCOUNT created from the app (not a backtest): {label.strip()[:80]}; "
                                       f"symbol {symbol.strip().upper()}; starting cash {float(starting_cash):.2f}")
        reg.save_portfolio(exp, Portfolio.reconstruct(cash=float(starting_cash), positions=[]))
        return exp

    def run_cycle(self, cfg: PaperRunConfig, *, request_token: str | None = None, monitoring: bool = True,
                  approved_config_hash: str | None = None) -> RunOutcome:
        """Run exactly ONE bounded paper cycle through the authoritative runtime.

        The same hard-FAIL preflight gate as `paper run --once` applies (no data request). A repeated `request_token`
        returns the earlier outcome without calling the runtime again.
        """
        with self._lock:
            if request_token is not None and request_token in self._tokens:
                prior = self._tokens[request_token]
                return RunOutcome(prior.record, prior.refused, prior.refused_checks, prior.error, prior.locked, True)
            if approved_config_hash is not None and approved_config_hash != cfg.identity_hash():
                return RunOutcome(refused="the approved preflight does not match the configuration being run; run preflight again")
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

    def monitor_cycle(self, cfg: PaperRunConfig, cycle_id: str) -> RunOutcome:
        """EXPLICIT monitoring-only operation (no strategy, no order, no cycle, no portfolio change). A completed monitoring
        step is never repeated. The recorded result is flagged `late` and describes state as of now."""
        with self._lock:
            try:
                return RunOutcome(record=self.runtime(cfg, monitoring=True).monitor_cycle(cycle_id))
            except (PaperSetupError, PaperStoreError) as exc:
                return RunOutcome(refused=str(exc)[:300])
            except PaperLockedError as exc:
                return RunOutcome(refused=str(exc)[:300], locked=True)
            except Exception as exc:  # noqa: BLE001
                return RunOutcome(error=f"{type(exc).__name__}: {str(exc)[:300]}")

    # ------------------------------------------------------------------ Sprint 20: integrated run, proposals
    def run_with_preflight(self, cfg: PaperRunConfig, *, request_token: str | None = None, monitoring: bool = True,
                           progress: Callable[[str, str, str], None] | None = None) -> RunOutcome:
        """The routine "Run Paper Cycle": a FRESH preflight (one market-data request, reused by the cycle) gates ONE bounded
        cycle through the authoritative runtime. Failed or unevaluable safety checks block; nothing is executed then. An
        actionable, risk-approved signal ends as a persisted PROPOSAL -- never an order. `progress(stage, status, detail)`
        is called only after the underlying call returns."""
        say = progress or (lambda *_: None)
        with self._lock:
            if request_token is not None and request_token in self._tokens:
                prior = self._tokens[request_token]
                return RunOutcome(prior.record, prior.refused, prior.refused_checks, prior.error, prior.locked, True,
                                  prior.preflight, prior.proposal_error)
            outcome = self._run_integrated(cfg, monitoring, say)
            if request_token is not None:
                self._tokens[request_token] = outcome
            return outcome

    def _run_integrated(self, cfg: PaperRunConfig, monitoring: bool, say) -> RunOutcome:
        try:
            rt = self.runtime(cfg, monitoring=monitoring)
            rep = rt.preflight(check_data=True)
            block = blocking_checks(rep)
            say("preflight", "BLOCKED" if block else "PASSED", f"{len(block)} blocking check(s)" if block else "all safety-critical checks passed")
            if block:
                return RunOutcome(refused="the fresh preflight found blocking problems; nothing was executed",
                                  refused_checks=[c.to_dict() for c in block], preflight=rep.to_dict())
            rec = rt.run_cycle()
            say("cycle", rec.status.value, rec.reason_code or "")
            return RunOutcome(record=rec, preflight=rep.to_dict())
        except (PaperSetupError, PaperStoreError) as exc:
            say("cycle", "REFUSED", str(exc)[:120])
            return RunOutcome(refused=str(exc)[:300])
        except PaperLockedError as exc:
            say("cycle", "LOCKED", str(exc)[:120])
            return RunOutcome(refused=str(exc)[:300], locked=True)
        except Exception as exc:  # noqa: BLE001 -- surfaced, never reported as success; the cycle record may exist
            say("cycle", "ERROR", type(exc).__name__)
            return RunOutcome(error=f"{type(exc).__name__}: {str(exc)[:300]}")

    def _proposal_runtime(self, proposal: ProposalRecord, *, monitoring: bool = True) -> PaperTradingRuntime:
        cfg = PaperRunConfig.from_dict(proposal.config) if proposal.config else None
        if cfg is None:
            raise PaperSetupError("the proposal does not carry the configuration it was made under; run a fresh cycle")
        return self.runtime(cfg, monitoring=monitoring)

    def proposals(self, experiment_id: int, *, pending_only: bool = False, limit: int = 50) -> list[dict]:
        """READ-ONLY proposal rows with an EFFECTIVE status: a PENDING proposal past its expiry is shown EXPIRED (the stored
        state is only written by the next explicit action; viewing mutates nothing)."""
        s = self.store()
        if s is None:
            return []
        clock = self._runtime_kwargs.get("clock")
        now = datetime.now(timezone.utc) if clock is None else clock()
        out = []
        for p in s.list_proposals(experiment_id, (ProposalStatus.PENDING, ProposalStatus.APPROVING) if pending_only else None, limit):
            eff = p.status.value
            if p.status is ProposalStatus.PENDING and datetime.fromisoformat(p.expires_at) <= now:
                eff = "EXPIRED"
            if pending_only and eff == "EXPIRED":
                continue
            out.append({"proposal": p, "effective_status": eff})
        return out

    def approve_proposal(self, proposal_id: str, experiment_id: int, *, monitoring: bool = True) -> RunOutcome:
        """Approve ONE exact persisted proposal. All conditions are re-validated by the runtime; the order (if any) goes through
        the ordinary durable-intent -> PaperBroker path. Idempotent."""
        with self._lock:
            s = self.store()
            prop = s.get_proposal(proposal_id) if s else None
            if prop is None or prop.experiment_id != experiment_id:
                return RunOutcome(refused="that proposal does not exist for the selected account", proposal_error="NOT_FOUND")
            try:
                return RunOutcome(record=self._proposal_runtime(prop, monitoring=monitoring).approve_proposal(proposal_id))
            except PaperProposalError as exc:
                return RunOutcome(refused=str(exc), proposal_error=exc.code)
            except (PaperSetupError, PaperStoreError) as exc:
                return RunOutcome(refused=str(exc)[:300], proposal_error="SETUP")
            except PaperLockedError as exc:
                return RunOutcome(refused=str(exc)[:300], locked=True, proposal_error="LOCKED")
            except Exception as exc:  # noqa: BLE001
                return RunOutcome(error=f"{type(exc).__name__}: {str(exc)[:300]}")

    def reject_proposal(self, proposal_id: str, experiment_id: int) -> RunOutcome:
        with self._lock:
            s = self.store()
            prop = s.get_proposal(proposal_id) if s else None
            if prop is None or prop.experiment_id != experiment_id:
                return RunOutcome(refused="that proposal does not exist for the selected account", proposal_error="NOT_FOUND")
            try:
                return RunOutcome(record=self._proposal_runtime(prop, monitoring=False).reject_proposal(proposal_id, "rejected by user in the app"))
            except PaperProposalError as exc:
                return RunOutcome(refused=str(exc), proposal_error=exc.code)
            except (PaperSetupError, PaperStoreError) as exc:
                return RunOutcome(refused=str(exc)[:300], proposal_error="SETUP")
            except PaperLockedError as exc:
                return RunOutcome(refused=str(exc)[:300], locked=True, proposal_error="LOCKED")
            except Exception as exc:  # noqa: BLE001
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
        mon = c.monitoring
        rid = (mon or {}).get("run_id")
        run_missing = None
        if rid and self.paths.monitoring_exists():
            from src.monitoring.store import MonitoringStore
            ms = MonitoringStore(self.paths.monitoring_db)
            run = ms.get_run(rid)
            if run is not None:
                findings = [f for f in (ms.get_finding(i) for i in run.finding_ids) if f is not None]
                events = ms.alerts.events_for_run(rid)
            else:
                run_missing = "The monitoring run id is recorded on the cycle but that run is not in the monitoring database."
        elif rid:
            run_missing = "The monitoring run id is recorded on the cycle but no monitoring database exists to look it up in."
        mstate = monitoring_state(mon, run_found=run is not None, run_missing=run_missing)
        if mstate["state"] != "COMPLETED":
            limits.append(mstate["message"])
        return {"available": True, "cycle": c, "intent": intent, "fill": fill, "reconciliation": recon,
                "monitoring_run": run, "findings": findings, "alert_events": events, "monitoring_state": mstate,
                "limitations": limits}
