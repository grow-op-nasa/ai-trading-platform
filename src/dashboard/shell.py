"""Application shell: navigation, PAPER MODE indicator, compact readiness, account selection (Sprint 19)."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.appsvc import prefs, research
from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.appsvc.system import FAIL, OK, WARN, collect_status, overall

PAGES = ("Home", "Opportunity Scanner", "Research & Experiments", "Strategies", "Paper Trading", "Portfolio & Execution",
         "Activity", "Monitoring & Alerts", "Setup & System Status")
DESCRIPTIONS = {
    "Home": "Your paper account, what needs attention, and the next safe step. Read-only: opening it runs nothing.",
    "Opportunity Scanner": "Run one registered strategy over a watchlist (daily bars) and see which symbols have a signal. Read-only: it never places orders or creates paper records.",
    "Activity": "One chronological view of stored cycles, proposals, orders, fills, reconciliations, monitoring and alerts. Read-only.",
    "Research & Experiments": "Saved historical backtests: identity, metrics, equity and comparison. Not paper trading, not live state.",
    "Strategies": "Strategies registered in the production registry (the only ones usable for paper runs) and development candidates for context.",
    "Paper Trading": "One-click guided run (fresh preflight included), human approval of proposed trades, and cycle inspection. Paper only.",
    "Portfolio & Execution": "Stored portfolio, explicit valuation, paper intents, fills, fees and reconciliation.",
    "Monitoring & Alerts": "Monitoring runs, findings, check status and alert delivery. Delivery status is separate from severity.",
    "Setup & System Status": "Configuration and readiness (no secrets), and what is and is not running.",
}


def paper_service(paths: AppPaths) -> PaperService:
    svc = st.session_state.get("_paper_service")
    if svc is None or svc.paths != paths:
        svc = PaperService(paths)
        st.session_state["_paper_service"] = svc
    return svc


def scanner_service(paths: AppPaths):
    from src.appsvc.scanner import ScannerService
    svc = st.session_state.get("_scanner_service")
    if svc is None or svc.paths != paths:
        svc = ScannerService(paths, paper_service=paper_service(paths))
        st.session_state["_scanner_service"] = svc
    return svc


@st.cache_data(ttl=10, show_spinner=False)
def _experiments(registry: str, marker: float):
    return research.experiments(AppPaths(registry=Path(registry)))


def experiment_rows(paths: AppPaths):
    if not paths.registry_exists():
        return []
    return _experiments(str(paths.registry), paths.registry.stat().st_mtime)


def goto(page: str) -> None:
    """on_click callback: change the current view. Navigation only -- it runs nothing."""
    if page in PAGES:
        st.session_state["page"] = page


def _persist_account(paths: AppPaths) -> None:
    """on_change callback: remember an EXPLICIT selection. Selection never mutates or initialises a portfolio."""
    value = st.session_state.get("account_id")
    if value is None:
        return
    try:
        prefs.save_account(paths, int(value))
        st.session_state.pop("_pref_error", None)
        st.session_state["_acct_res"] = prefs.AccountResolution(int(value), prefs.SELECTED, f"Account #{value} selected.", int(value))
    except OSError as exc:
        st.session_state["_pref_error"] = f"Could not save your account choice ({type(exc).__name__}); it applies to this session only."


def sidebar(paths: AppPaths) -> tuple[str, int | None]:
    st.sidebar.title("AI Trading Platform")
    st.sidebar.warning("PAPER MODE -- simulated fills only. There is no live broker connection and no path to one.")
    if st.session_state.get("page") not in PAGES:
        st.session_state["page"] = PAGES[0]
    page = st.sidebar.radio("View", PAGES, key="page")
    rows = [r for r in experiment_rows(paths) if r.has_portfolio]
    ids = [r.experiment_id for r in rows]
    if "_acct_res" not in st.session_state:             # first load of this session: restore, never guess
        res = prefs.resolve_account(paths, ids)
        st.session_state["_acct_res"] = res
        if res.selected is not None:
            st.session_state["account_id"] = res.selected
    if st.session_state.get("account_id") not in ids:
        st.session_state.pop("account_id", None)         # a vanished account is never silently substituted
    account = None
    if rows:
        account = st.sidebar.selectbox(
            "Paper account (experiment)", ids, index=None, placeholder="Select an account", key="account_id",
            on_change=_persist_account, args=(paths,),
            format_func=lambda i: f"#{i}  {next(r.strategy_name for r in rows if r.experiment_id == i)}")
    res = st.session_state["_acct_res"]
    if account is None and res.state in (prefs.SAVED_MISSING, prefs.MALFORMED):
        st.sidebar.warning(res.message)
    if st.session_state.get("_pref_error"):
        st.sidebar.warning(st.session_state["_pref_error"])
    items = collect_status(paths)
    state = overall(items)
    label = {OK: "Readiness: OK", WARN: "Readiness: attention needed", FAIL: "Readiness: problem"}[state]
    (st.sidebar.error if state == FAIL else st.sidebar.warning if state == WARN else st.sidebar.success)(label + " (see Setup & System Status)")
    st.sidebar.caption("Data-source health is only checked when you run; portfolio and risk health are on Home.")
    return page, account


def header(page: str) -> None:
    st.header(page)
    st.caption(DESCRIPTIONS[page])


def need_account(account: int | None) -> bool:
    if account is None:
        st.info("Select a paper account (an experiment with a saved portfolio) in the sidebar. Nothing is selected or created for you.")
        return False
    return True
