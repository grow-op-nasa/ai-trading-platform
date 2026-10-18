"""Homepage state (Sprint 20, ADR-0056): ONE read-only summary of the selected paper account and the platform.

Building a HomeState reads stored files only. It requests no market data, runs no preflight, creates no account or
database, starts no scheduler, sends no email and calls no AI. Every field says where it came from; anything that could
not be read is reported as unavailable, never as healthy. The contextual primary action is a deterministic decision list
whose first matching rule wins -- it only NAMES the next safe step and the page that does it; it executes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.appsvc import explain
from src.appsvc import monitoring as mon
from src.appsvc import portfolio as pf
from src.appsvc import prefs, research, strategies
from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.appsvc.system import FAIL, OK, UNKNOWN, WARN, collect_status

HOME, PAPER, PORTFOLIO, MONITORING, SETUP = ("Home", "Paper Trading", "Portfolio & Execution", "Monitoring & Alerts",
                                              "Setup & System Status")


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    label: str
    required: bool
    state: str            # DONE | TODO | PROBLEM | OPTIONAL_OFF | OPTIONAL_ON
    detail: str
    next_step: str = ""
    page: str = ""        # where the next step is performed


@dataclass(frozen=True)
class PrimaryAction:
    label: str
    page: str
    reason: str
    kind: str = "INFO"    # INFO | WARN | FAIL


@dataclass(frozen=True)
class HomeState:
    resolution: prefs.AccountResolution
    account: int | None
    account_label: str = ""
    cash: float | None = None
    open_positions: int | None = None
    realized_pnl: float | None = None
    stored_snapshot_note: str = "Stored snapshot from the experiment registry (not live; positions at cost; no price requested)."
    initialised: bool | None = None
    latest_cycle: Any = None
    latest_cycle_explanation: explain.Explanation | None = None
    pending_proposals: tuple = ()
    unresolved_intents: int = 0
    reconciliation: Any = None
    monitoring_available: bool = False
    latest_monitoring_status: str = ""
    open_critical: int = 0
    open_warning: int = 0
    not_evaluable: tuple[str, ...] = ()
    alert_counts: dict[str, int] | None = None
    alert_state: str = UNKNOWN
    readiness: str = OK
    checklist: tuple[ChecklistItem, ...] = ()
    primary: PrimaryAction | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def required_outstanding(self) -> list[ChecklistItem]:
        return [c for c in self.checklist if c.required and c.state != "DONE"]


def eligible_accounts(paths: AppPaths) -> list[Any]:
    return [r for r in research.experiments(paths) if r.has_portfolio]


def _checklist(paths: AppPaths, status: dict[str, Any], accounts: list[Any], account: int | None, initialised: bool | None) -> list[ChecklistItem]:
    out: list[ChecklistItem] = []
    add = lambda *a, **k: out.append(ChecklistItem(*a, **k))  # noqa: E731
    env_bad = [n for n in ("Python", "Dependencies") if status.get(n) is not None and status[n].state == FAIL]
    add("env", "Python and required packages", True, "PROBLEM" if env_bad else "DONE",
        "; ".join(status[n].detail for n in env_bad) if env_bad else "Python and required packages are present.",
        "Run scripts/setup_app.sh." if env_bad else "", SETUP)
    reg = paths.registry_exists()
    add("registry", "Experiment registry exists", True, "DONE" if reg else "TODO",
        "Found." if reg else "No experiment registry yet (the app never creates it).",
        "" if reg else "Create an experiment with `.venv/bin/python scripts/run_experiment.py`, then reload.", SETUP)
    n_strats = len(strategies.eligible_names(paths)) if reg else 0
    add("strategy", "A registered strategy is available", True, "DONE" if n_strats else "TODO",
        f"{n_strats} registered strategy(ies) eligible for paper runs." if n_strats else "No registered strategy found.",
        "" if n_strats else "Review the Strategies page; only registered strategies can be paper-run.", "Strategies")
    add("portfolio", "An experiment with a saved portfolio exists", True, "DONE" if accounts else "TODO",
        f"{len(accounts)} eligible experiment(s)." if accounts else "No experiment has a saved portfolio.",
        "" if accounts else "Run an experiment that saves a portfolio, then reload.", SETUP)
    add("account", "A paper account is selected", True, "DONE" if account is not None else "TODO",
        f"Account #{account} selected." if account is not None else "No account is selected.",
        "" if account is not None else "Choose an account in the sidebar. Nothing is selected or created for you.", HOME)
    if account is not None:
        add("init", "Paper account is initialised", True, "DONE" if initialised else "TODO",
            "Paper account exists in the paper database." if initialised else "This account has not been initialised for paper trading.",
            "" if initialised else "Use 'Set up paper account' on Paper Trading (explicit; never automatic).", PAPER)
    # optional -- must never make the app look unhealthy
    for key, label, name, off_text, on_next, page in (
        ("monitoring", "Monitoring database (optional)", "Monitoring database", "No monitoring run recorded yet; monitoring is optional and runs after a paper cycle.", "", MONITORING),
        ("alerts", "Email alert delivery (optional)", "Alert delivery", "Alert delivery is off. This is a normal default.", "Configure ATP_ALERT_* only if you want email.", SETUP),
        ("ai", "AI provider (optional)", "AI provider", "No AI key configured. Not needed for any paper-trading step.", "", SETUP),
    ):
        it = status.get(name)
        on = it is not None and it.state in (OK, WARN) and (key != "monitoring" or paths.monitoring_exists())
        bad = it is not None and it.state == FAIL
        add(key, label, False, "PROBLEM" if bad else "OPTIONAL_ON" if on else "OPTIONAL_OFF",
            (it.detail if (on or bad) and it else off_text), on_next if not on else "", page)
    return out


def _primary(res: prefs.AccountResolution, accounts: list[Any], reg: bool, account: int | None, initialised: bool | None,
             recon: Any, unresolved: int, pending: list, last: Any, outstanding: list[ChecklistItem]) -> PrimaryAction:
    envp = next((c for c in outstanding if c.key == "env"), None)
    if envp:
        return PrimaryAction("Review setup", SETUP, envp.detail, "FAIL")
    if not reg or not accounts:
        return PrimaryAction("Review setup", SETUP, "There is no experiment with a saved portfolio to use as a paper account yet.", "WARN")
    if account is None:
        why = res.message if res.state in (prefs.SAVED_MISSING, prefs.MALFORMED) else "No paper account is selected."
        return PrimaryAction("Select paper account", HOME, why + " Use the sidebar selector.", "WARN")
    if not initialised:
        return PrimaryAction("Set up paper account", PAPER, "The selected account has not been initialised for paper trading.", "WARN")
    if recon is not None and recon.status.value == "MISMATCH":
        return PrimaryAction("Review reconciliation", PORTFOLIO, "The latest reconciliation found a mismatch. New risk-taking is blocked until it is understood.", "FAIL")
    if unresolved:
        return PrimaryAction("Review reconciliation", PORTFOLIO, f"{unresolved} execution intent(s) are unresolved.", "FAIL")
    if pending:
        return PrimaryAction("Review proposed trade", PAPER, f"{len(pending)} proposed paper trade(s) are waiting for your decision. Nothing has been ordered.", "WARN")
    if last is not None and last.status.value in ("FAILED", "BLOCKED", "RECONCILIATION_REQUIRED", "AWAITING_EXECUTION", "ORDER_REJECTED", "PROPOSAL_INVALIDATED"):
        return PrimaryAction("Review cycle result", PAPER, f"The latest cycle ended {last.status.value}.", "WARN")
    return PrimaryAction("Go to Paper Trading", PAPER, "Everything required is in place. Run a paper cycle when you are ready.", "INFO")


def build(paths: AppPaths, svc: PaperService, account: int | None, resolution: prefs.AccountResolution | None = None,
          status_items: list | None = None) -> HomeState:
    accounts = eligible_accounts(paths)
    res = resolution or (prefs.AccountResolution(account, prefs.SELECTED if account is not None else prefs.NONE_SAVED, ""))
    status = {i.name: i for i in (status_items if status_items is not None else collect_status(paths))}
    reg = paths.registry_exists()
    initialised: bool | None = None
    stored = latest = recon = None
    pending: list = []
    unresolved = 0
    label = ""
    notes: list[str] = []
    if account is not None:
        row = next((r for r in accounts if r.experiment_id == account), None)
        label = f"#{account}  {row.strategy_name}" if row else f"#{account}"
        stored = pf.stored_state(paths, account)
        if stored is None:
            notes.append(f"Experiment #{account} has no saved portfolio.")
        info = svc.inspect(account)
        if info["available"]:
            initialised = info["account"] is not None
            if initialised:
                latest = info["cycles"][0] if info["cycles"] else None
                unresolved = len(info["unresolved"])
                recon = info["latest_reconciliation"]
                pending = [p for p in svc.proposals(account, pending_only=True)]
        else:
            initialised = False
    checklist = _checklist(paths, status, accounts, account, initialised)
    outstanding = [c for c in checklist if c.required and c.state != "DONE"]
    primary = _primary(res, accounts, reg, account, initialised, recon, unresolved, pending, latest, outstanding)

    run_status, crit, warn, ne = "", 0, 0, ()
    m_av = mon.available(paths)
    counts = None
    if m_av:
        runs = mon.runs(paths, 1)
        if runs:
            run_status = runs[0].status.value
            ne = tuple(c.check_id for c in runs[0].checks if c.status.value == "NOT_EVALUABLE")
        o = mon.open_findings(paths)
        crit, warn = len(o["CRITICAL"]), len(o["WARNING"])
        counts = mon.alert_counts(paths)
    alert_item = status.get("Alert delivery")
    return HomeState(
        resolution=res, account=account, account_label=label,
        cash=None if stored is None else stored.cash, open_positions=None if stored is None else len(stored.positions),
        realized_pnl=None if stored is None else stored.realized_pnl, initialised=initialised,
        latest_cycle=latest, latest_cycle_explanation=explain.cycle(latest.status.value, latest.reason_code) if latest else None,
        pending_proposals=tuple(pending), unresolved_intents=unresolved, reconciliation=recon,
        monitoring_available=m_av, latest_monitoring_status=run_status, open_critical=crit, open_warning=warn, not_evaluable=ne,
        alert_counts=counts, alert_state=alert_item.state if alert_item else UNKNOWN,
        readiness=FAIL if any(c.state == "PROBLEM" and c.required for c in checklist) else WARN if outstanding else OK,
        checklist=tuple(checklist), primary=primary, notes=tuple(notes))
