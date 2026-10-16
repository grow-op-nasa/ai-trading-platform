"""Application shell: navigation, PAPER MODE indicator, compact readiness, account selection (Sprint 19)."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.appsvc import research
from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.appsvc.system import FAIL, OK, WARN, collect_status, overall

PAGES = ("Overview", "Research & Experiments", "Strategies", "Paper Trading", "Portfolio & Execution",
         "Monitoring & Alerts", "Setup & System Status")
DESCRIPTIONS = {
    "Overview": "Stored paper-account state, the latest cycle, reconciliation and monitoring at a glance.",
    "Research & Experiments": "Saved historical backtests: identity, metrics, equity and comparison. Not paper trading, not live state.",
    "Strategies": "Strategies registered in the production registry (the only ones usable for paper runs) and development candidates for context.",
    "Paper Trading": "Preflight, one explicit paper cycle through the Sprint 18 runtime, and cycle inspection. Paper only.",
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


@st.cache_data(ttl=10, show_spinner=False)
def _experiments(registry: str, marker: float):
    return research.experiments(AppPaths(registry=Path(registry)))


def experiment_rows(paths: AppPaths):
    if not paths.registry_exists():
        return []
    return _experiments(str(paths.registry), paths.registry.stat().st_mtime)


def sidebar(paths: AppPaths) -> tuple[str, int | None]:
    st.sidebar.title("AI Trading Platform")
    st.sidebar.warning("PAPER MODE -- simulated fills only. There is no live broker connection and no path to one.")
    page = st.sidebar.radio("View", PAGES)
    rows = [r for r in experiment_rows(paths) if r.has_portfolio]
    account = None
    if rows:
        account = st.sidebar.selectbox(
            "Paper account (experiment)", [r.experiment_id for r in rows], index=None, placeholder="Select an account",
            format_func=lambda i: f"#{i}  {next(r.strategy_name for r in rows if r.experiment_id == i)}")
    items = collect_status(paths)
    state = overall(items)
    label = {OK: "Readiness: OK", WARN: "Readiness: attention needed", FAIL: "Readiness: problem"}[state]
    (st.sidebar.error if state == FAIL else st.sidebar.warning if state == WARN else st.sidebar.success)(label + " (see Setup & System Status)")
    st.sidebar.caption("Data-source health is only checked when you run a preflight or valuation; portfolio and risk health are on the Overview.")
    return page, account


def header(page: str) -> None:
    st.header(page)
    st.caption(DESCRIPTIONS[page])


def need_account(account: int | None) -> bool:
    if account is None:
        st.info("Select a paper account (an experiment with a saved portfolio) in the sidebar. Nothing is selected or created for you.")
        return False
    return True
