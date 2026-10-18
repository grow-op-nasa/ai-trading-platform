"""Smoke tests for `src.dashboard` -- Sprint 9 (`DECISIONS.md`, ADR-0042), extended in Sprint 19 (ADR-0054) for the
seven-page local application shell.

Uses Streamlit's own supported testing facility
(`streamlit.testing.v1.AppTest`) rather than a brittle, pixel-level UI
test -- explicitly out of scope (Sprint 9 spec). Skips cleanly wherever
Streamlit isn't installed (`pytest.importorskip`) -- this sandbox has
no network access to install it, so these tests are written to run for
real on the dev machine, where `streamlit==1.59.2` is pinned in
`requirements.txt`.

Fully network-free (Sprint 9 spec, section 33): `MarketDataService.
get_history` is monkeypatched at the class level before `AppTest.run()`
ever executes `src/dashboard/app.py`, so even a paper portfolio with
open positions never reaches a real provider. No API keys, no broker
credentials, no LLM call -- any research report shown is whatever
`ResearchReporter` already rendered and persisted at
`run_experiment()` time (the deterministic fallback renderer, since no
`ANTHROPIC_API_KEY` is set in a test environment).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("streamlit")

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from scripts.run_experiment import run_experiment  # noqa: E402
from src.data.exceptions import NoDataError  # noqa: E402
from src.data.service import MarketDataService  # noqa: E402
from src.appsvc.paper import PaperService  # noqa: E402
from src.appsvc.paths import AppPaths  # noqa: E402
from src.dashboard.shell import PAGES  # noqa: E402
from tests.test_paper_runtime import _reset_script  # noqa: E402,F401  (autouse: clears scripted-strategy state)
from src.experiments.registry import ExperimentRegistry  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parent.parent / "src" / "dashboard" / "app.py")

EMA_CLOSES = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120, 110, 100, 90, 80]
RSI_CLOSES = [100 - i * 2 for i in range(15)] + [70 + i * 3 for i in range(15)]

# A pure, unbroken decline -- unlike RSI_CLOSES (whose second half recovers
# enough to close the position out again by the end: confirmed empirically,
# not just by comment, after a real dashboard-smoke run showed 0 open
# positions and a realized P&L for that series). `src.indicators.formulas.
# relative_strength_index` computes RSI via `ewm(alpha=1/period)` over gains
# and losses, so a series with *zero gains ever* pins `avg_gain` at 0 and
# therefore RSI at exactly 0 from the second bar onward, well under any
# `oversold` threshold -- `RSIMeanReversionStrategy` enters LONG almost
# immediately and, since RSI can never climb back to `overbought` without a
# single up-day, never exits. Used wherever a test genuinely needs an open
# position at the end of the run, not just a plausible-looking one.
OPEN_POSITION_CLOSES = [100 - i * 2 for i in range(15)]


def make_candles(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )


@pytest.fixture(autouse=True)
def _no_real_market_data(monkeypatch):
    """The one sanctioned network touchpoint (`MarketDataService.
    get_history`, via `src.analytics.valuation.latest_price`) is
    monkeypatched for every test in this file -- a real network call
    from a test suite would violate Sprint 9 spec section 33 even if it
    happened to succeed."""

    def _fake_get_history(self, symbol, period="5d", interval="1d"):
        return pd.DataFrame({"close": [123.45]})

    monkeypatch.setattr(MarketDataService, "get_history", _fake_get_history)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """`ExperimentRegistry.DEFAULT_DB_PATH` is the relative path
    `"data/experiments.db"` -- `chdir` into an isolated tmp_path so the
    dashboard (which uses that same default) and the fixture data this
    test populates always agree on which database file they mean,
    without touching the real project's `data/experiments.db`.

    `src.dashboard.views`'s `st.cache_data`-decorated loaders key their
    cache purely on the (relative) db_path string passed in -- they have
    no way to know two tests used that identical string for two
    different `tmp_path` directories. Without clearing the cache here,
    a later test in this file can silently read another test's cached
    (and by then stale) experiment list, since every test in this
    module runs well inside the loaders' 30-60s TTL. This is a test-
    isolation fix only: in real single-deployment use there's exactly
    one `data/experiments.db`, so no such collision is possible."""
    monkeypatch.chdir(tmp_path)
    st.cache_data.clear()
    return tmp_path


def _populate_one_experiment(symbol="SPY", strategy="ema_cross", params=None, closes=None):
    registry = ExperimentRegistry()  # default path, relative to the chdir'd cwd
    candles = make_candles(closes or EMA_CLOSES)
    return run_experiment(
        strategy_name=strategy,
        symbol=symbol,
        candles=candles,
        strategy_params=params or {"fast": 2, "slow": 4},
        registry=registry,
    )


def _assert_no_exception(at: AppTest) -> None:
    assert not at.exception, [e.message for e in at.exception]


# ---------------------------------------------------------------------------
# Renders without crashing, in every state
# ---------------------------------------------------------------------------


# AppTest's own default is a 3s per-run timeout, tuned for trivial
# scripts -- too tight for pages that genuinely compute analytics,
# value a portfolio, and draw several charts against real Streamlit
# (as opposed to the sandbox's now-retired hand-rolled stub, which was
# never timed). A generous, fixed timeout here is a test-tooling
# accommodation, not evidence of a slow page in production (a real
# Streamlit server keeps the app warm between reruns; AppTest
# re-executes the whole script from scratch on every `.run()`).
_RUN_TIMEOUT = 15



def _go(at: AppTest, page: str) -> AppTest:
    at.sidebar.radio[0].set_value(page).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    return at


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def _checkbox(at: AppTest, label: str):
    return next(c for c in at.checkbox if c.label == label)


# ---------------------------------------------------------------------------
# Empty state: every page renders, nothing is created or fabricated
# ---------------------------------------------------------------------------


def test_every_page_renders_with_no_data_and_creates_nothing(workdir):
    at = AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    for page in PAGES:
        _go(at, page)
    assert not (workdir / "data").exists(), "browsing must never create a database, directory, portfolio or profile"
    assert any("PAPER MODE" in w.value for w in at.sidebar.warning)


def test_home_is_the_default_landing_page_and_explains_the_next_step(workdir):
    at = AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert at.sidebar.radio(key="page").value == "Home" and PAGES[0] == "Home" and len(PAGES) == 9
    assert any("Next step" in x.value and "Review setup" in x.value for x in list(at.warning) + list(at.error) + list(at.info))
    assert any(b.label == "Review setup" for b in at.button)
    assert not (workdir / "data").exists()


def test_research_page_with_no_experiments_says_so(workdir):
    at = _go(AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT), "Research & Experiments")
    assert any("No experiments found" in i.value for i in at.info)


# ---------------------------------------------------------------------------
# Research, strategies, setup
# ---------------------------------------------------------------------------


def test_research_page_shows_analysis_metrics_and_historical_label(workdir):
    run_result = _populate_one_experiment()
    at = _go(AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT), "Research & Experiments")
    assert any(str(run_result.experiment_id) in h.value for h in at.header)
    assert {"Total return", "Sharpe ratio", "Max drawdown", "Win rate"} <= {m.label for m in at.metric}
    assert any("HISTORICAL BACKTEST" in c.value for c in at.caption)


def test_research_comparison_tab_flags_differences(workdir):
    a = _populate_one_experiment(symbol="SPY", strategy="ema_cross")
    b = _populate_one_experiment(symbol="QQQ", strategy="rsi_mean_reversion", params={"period": 5}, closes=RSI_CLOSES)
    at = _go(AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT), "Research & Experiments")
    at.multiselect(key="research_compare").set_value([a.experiment_id, b.experiment_id]).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert len(at.warning) >= 1


def test_strategies_page_lists_registry_and_marks_candidates_ineligible(workdir):
    at = _go(AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT), "Strategies")
    text = " ".join(df.value.to_string() for df in at.dataframe)
    assert "ema_cross" in text and "rsi_mean_reversion" in text
    assert any("never be selected for a paper run" in c.value for c in at.caption)


def test_setup_page_reports_state_without_secrets(workdir, monkeypatch):
    secret = "sk-ant-" + "NEVER-SHOW-THIS"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    at = _go(AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT), "Setup & System Status")
    rendered = " ".join(x.value for x in list(at.success) + list(at.warning) + list(at.error) + list(at.info) + list(at.markdown))
    assert "AI provider" in rendered and secret not in rendered


# ---------------------------------------------------------------------------
# Portfolio: stored snapshot; valuation only on an explicit click
# ---------------------------------------------------------------------------


def _open_position_experiment():
    return _populate_one_experiment(symbol="QQQ", strategy="rsi_mean_reversion", params={"period": 5},
                                    closes=OPEN_POSITION_CLOSES)


def test_portfolio_page_shows_stored_positions_and_requests_no_price_on_view(workdir, monkeypatch):
    calls = []
    monkeypatch.setattr(MarketDataService, "get_history", lambda self, *a, **k: calls.append(a) or pd.DataFrame({"close": [1.0]}))
    run_result = _open_position_experiment()
    assert len(ExperimentRegistry().get_portfolio(run_result.experiment_id).positions) == 1
    at = AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT)
    at.sidebar.selectbox[0].set_value(run_result.experiment_id).run(timeout=_RUN_TIMEOUT)
    _go(at, "Portfolio & Execution")
    assert any("QQQ" in df.value.to_string() for df in at.dataframe)
    assert any("Not valued" in i.value for i in at.info)         # equity is unknown, not zero
    assert calls == []


def test_valuation_runs_only_on_click_and_unpriced_equity_is_undefined(workdir, monkeypatch):
    def _raise_no_data(self, symbol, period="5d", interval="1d"):
        raise NoDataError("no data available")
    monkeypatch.setattr(MarketDataService, "get_history", _raise_no_data)
    run_result = _open_position_experiment()
    at = AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT)
    at.sidebar.selectbox[0].set_value(run_result.experiment_id).run(timeout=_RUN_TIMEOUT)
    _go(at, "Portfolio & Execution")
    _button(at, "Value open positions now (requests one price per open symbol)").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    equity = next(m for m in at.metric if m.label == "Equity")
    assert "$" not in equity.value                 # undefined, never a partial or zero dollar figure
    assert len(at.warning) >= 1


def test_rendering_never_mutates_the_saved_portfolio(workdir):
    run_result = _open_position_experiment()
    registry = ExperimentRegistry()
    before = registry.get_portfolio(run_result.experiment_id)
    before_cash, before_open = before.cash, {p.symbol: p.quantity for p in before.positions.values()}
    assert before_open
    at = AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT)
    at.sidebar.selectbox[0].set_value(run_result.experiment_id).run(timeout=_RUN_TIMEOUT)
    for page in PAGES:
        _go(at, page)
    after = registry.get_portfolio(run_result.experiment_id)
    assert after.cash == before_cash and {p.symbol: p.quantity for p in after.positions.values()} == before_open
    assert not (workdir / "data" / "paper").exists() and not (workdir / "data" / "monitoring").exists()


# ---------------------------------------------------------------------------
# Paper trading through the real runtime (synthetic fixture world; fills are SIMULATED fixture fills)
# ---------------------------------------------------------------------------


@pytest.fixture
def paper_world(workdir):
    from tests.test_appsvc import service_for
    from tests.test_paper_runtime import STRAT, WED, World
    w = World(workdir / "data")
    w.script(WED, "LONG")
    svc = service_for(w)
    svc.paths = AppPaths()                       # the app's own (relative) paths; same files, as cwd is the workdir
    svc.save_profile("fixture", svc.build_config(experiment_id=w.exp, strategy_name=STRAT, symbol="QQQ"))
    at = AppTest.from_file(APP_PATH)
    at.session_state["_paper_service"] = svc
    at.run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    return w, svc, at


def _pick_symbol(at, symbol):
    """Operate the instrument widgets as a user does: a preset, or Custom… plus the text field."""
    from src.appsvc import instruments
    if symbol in instruments.symbols():
        at.selectbox(key="paper_symbol_choice").set_value(symbol).run(timeout=_RUN_TIMEOUT)
    else:
        at.selectbox(key="paper_symbol_choice").set_value(instruments.CUSTOM).run(timeout=_RUN_TIMEOUT)
        at.text_input(key="paper_symbol_custom").set_value(symbol).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)


def _select_all(at, w, symbol="QQQ"):
    """Operate the visible selectors exactly as a user does (no profile, no service-side setup)."""
    from tests.test_paper_runtime import STRAT
    at.selectbox(key="paper_strategy").set_value(STRAT).run(timeout=_RUN_TIMEOUT)
    at.selectbox(key="paper_account").set_value(w.exp).run(timeout=_RUN_TIMEOUT)
    _pick_symbol(at, symbol)
    _assert_no_exception(at)


def _run(at):
    """The primary action: ONE click; the fresh preflight is performed inside it (no separate preflight click)."""
    _button(at, "Run paper cycle").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)


def _approve(at, pid, *, confirm=True):
    if confirm:
        at.checkbox(key=f"appr_ok_{pid}").check()
    at.button(key=f"appr_approve_{pid}").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)


def _pending(svc, w):
    return svc.proposals(w.exp, pending_only=True)


def test_selectors_are_visible_and_populated_from_the_production_registry(paper_world):
    from tests.test_paper_runtime import STRAT
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    strat = at.selectbox(key="paper_strategy")
    assert STRAT in strat.options and strat.value is None             # populated from the registry, nothing preselected
    from src.appsvc.strategies import eligible_names
    assert list(strat.options) == eligible_names(AppPaths())
    assert any(o.startswith(f"#{w.exp} ") for o in at.selectbox(key="paper_account").options)
    assert at.selectbox(key="paper_interval").options == ["1d"]
    assert not any(b.label == "Run paper cycle" for b in at.button)    # only offered once the selections are complete
    _select_all(at, w)
    assert _button(at, "Run paper cycle").disabled is False             # no separate preflight click is required
    assert any(b.label == "Preflight only (read-only)" for b in at.button)


def test_ema_cross_is_offered_only_because_it_is_registered():
    import src.strategies  # noqa: F401
    from src.appsvc.strategies import eligible_names
    from src.strategies.registry import available_strategies
    assert set(eligible_names(AppPaths())) == set(available_strategies())


def test_standalone_preflight_is_read_only_and_runs_only_on_an_explicit_click(paper_world, monkeypatch):
    w, svc, at = paper_world
    calls = []
    real = svc.preflight
    monkeypatch.setattr(svc, "preflight", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    _go(at, "Paper Trading")
    _select_all(at, w)
    at.run(timeout=_RUN_TIMEOUT); at.run(timeout=_RUN_TIMEOUT)
    assert calls == []
    at.button(key="btn_preflight").click().run(timeout=_RUN_TIMEOUT)
    assert len(calls) == 1
    at.run(timeout=_RUN_TIMEOUT); at.run(timeout=_RUN_TIMEOUT)          # reruns never repeat it
    assert len(calls) == 1
    assert w.store().list_cycles(w.exp) == [] and w.store().list_proposals(w.exp) == []     # it changed nothing


def test_run_performs_its_own_fresh_preflight_and_ends_as_a_proposal_not_an_order(paper_world, monkeypatch):
    w, svc, at = paper_world
    standalone = []
    real = svc.preflight
    monkeypatch.setattr(svc, "preflight", lambda *a, **k: (standalone.append(1), real(*a, **k))[1])
    _go(at, "Paper Trading")
    _select_all(at, w)
    _run(at)
    assert standalone == []                                                  # no separate preflight click or call
    assert len(w.store().list_cycles(w.exp)) == 1 and len(_pending(svc, w)) == 1
    assert w.store().list_intents(w.exp) == [] and w.store().list_fills(w.exp) == []      # NO order yet
    assert any("AWAITING_APPROVAL" in x.value for x in at.warning)
    assert any("NOT placed" in x.value for x in at.warning)
    at.run(timeout=_RUN_TIMEOUT); at.run(timeout=_RUN_TIMEOUT)               # reruns never repeat the cycle
    assert len(w.store().list_cycles(w.exp)) == 1 and len(_pending(svc, w)) == 1


def test_changing_a_selection_never_reuses_an_earlier_standalone_preflight(paper_world):
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    _select_all(at, w)
    at.button(key="btn_preflight").click().run(timeout=_RUN_TIMEOUT)
    _pick_symbol(at, "SPY")
    assert any("different selections" in x.value for x in at.caption)


def test_failed_fresh_preflight_blocks_the_run_with_an_actionable_explanation(paper_world):
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    class _Down:
        def get_candles(self, *a, **k):
            raise NoDataError("provider unavailable (test)")
        fetch_candles = get_history = get_dataset = get_candles
    svc._market_data = _Down()                                         # market data cannot be obtained
    _select_all(at, w)
    _run(at)
    assert any("Not run" in e.value for e in at.error)
    assert any("market_data" in m.value for m in at.markdown)           # the failing check is named with its remediation
    assert w.store().list_cycles(w.exp) == [] and w.store().list_proposals(w.exp) == []


def test_approving_the_exact_proposal_places_one_simulated_order_and_reruns_never_repeat_it(paper_world):
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    _select_all(at, w)
    _run(at)
    pid = _pending(svc, w)[0]["proposal"].proposal_id
    _approve(at, pid)
    assert len(w.store().list_cycles(w.exp)) == 1 and len(w.store().list_fills(w.exp)) == 1 and len(w.store().list_intents(w.exp)) == 1
    assert any("ORDER_FILLED" in x.value for x in at.success)
    assert svc.proposals(w.exp)[0]["effective_status"] == "EXECUTED" and _pending(svc, w) == []
    at.run(timeout=_RUN_TIMEOUT); at.run(timeout=_RUN_TIMEOUT)
    assert len(w.store().list_fills(w.exp)) == 1 and len(w.monitor_calls) == 1
    at2 = AppTest.from_file(APP_PATH)                                     # a brand-new session (refresh) runs and approves nothing
    at2.session_state["_paper_service"] = svc
    at2.run(timeout=_RUN_TIMEOUT)
    _go(at2, "Paper Trading")
    assert len(w.store().list_fills(w.exp)) == 1


def test_approval_without_confirmation_and_rejection_place_no_order(paper_world):
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    _select_all(at, w)
    _run(at)
    pid = _pending(svc, w)[0]["proposal"].proposal_id
    _approve(at, pid, confirm=False)
    assert w.store().list_fills(w.exp) == [] and len(_pending(svc, w)) == 1
    assert any("Tick the confirmation" in x.value for x in at.warning)
    at.button(key=f"appr_reject_{pid}").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert w.store().list_fills(w.exp) == [] and w.store().list_intents(w.exp) == [] and _pending(svc, w) == []
    assert svc.proposals(w.exp)[0]["effective_status"] == "REJECTED"


def test_an_expired_proposal_cannot_be_approved_and_places_no_order(paper_world):
    from datetime import timedelta
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    _select_all(at, w)
    _run(at)
    pid = _pending(svc, w)[0]["proposal"].proposal_id
    w.now[0] = w.now[0] + timedelta(hours=2)                              # silence: the proposal lapses
    assert _pending(svc, w) == []
    assert svc.approve_proposal(pid, w.exp).proposal_error in ("EXPIRED", "NOT_PENDING")
    assert w.store().list_fills(w.exp) == [] and w.store().list_intents(w.exp) == []


def test_the_service_boundary_refuses_a_run_whose_approval_does_not_match(paper_world):
    w, svc, at = paper_world
    out = svc.run_cycle(w.config(), approved_config_hash="0" * 16)
    assert out.refused and "does not match" in out.refused
    assert w.store() is None or w.store().list_cycles(w.exp) == []


def test_no_signal_is_shown_as_a_valid_outcome_not_a_failure(paper_world):
    w, svc, at = paper_world
    from tests.test_paper_runtime import ScriptedStrategy
    ScriptedStrategy.SCRIPT.clear()                # no actionable signal on the decision bar
    _go(at, "Paper Trading")
    _select_all(at, w)
    _run(at)
    assert any("NO_ACTION" in i.value for i in at.info)
    # the fixture's fake monitor writes no monitoring database, so the page truthfully reports the linked run as missing;
    # that is the ONLY error allowed, and the cycle itself is shown as the valid outcome it is
    assert all("monitoring database" in e.value for e in at.error), [e.value for e in at.error]
    assert w.store().list_fills(w.exp) == []
    assert len(w.store().list_cycles(w.exp)) == 1


def test_no_paper_account_gives_a_clear_message_and_creates_nothing(workdir):
    at = _go(AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT), "Paper Trading")
    assert any("No paper account exists yet" in w.value for w in at.warning)
    assert any(e.label.startswith("No suitable paper account") for e in at.expander)
    assert not (workdir / "data").exists() or not any((workdir / "data").rglob("*.db"))


def test_creating_a_paper_account_is_explicit_and_selectable(workdir):
    import src.strategies  # noqa: F401
    from src.appsvc.strategies import eligible_names
    name = eligible_names(AppPaths())[0]
    at = _go(AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT), "Paper Trading")
    at.selectbox(key="new_acct_strategy").set_value(name)
    at.text_input(key="new_acct_symbol").set_value("QQQ")
    at.text_input(key="new_acct_label").set_value("ui test")
    _button(at, "Create paper account").click().run(timeout=_RUN_TIMEOUT)
    assert any("tick the confirmation" in x.value.lower() for x in at.warning)      # unticked: nothing created
    assert not AppPaths().registry_exists()
    at.checkbox(key="new_acct_ok").check()
    _button(at, "Create paper account").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert any("Created paper account" in x.value for x in at.success)
    at2 = _go(AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT), "Paper Trading")
    assert len(at2.selectbox(key="paper_account").options) == 1


def test_history_tab_traces_a_recorded_cycle(paper_world):
    w, svc, at = paper_world
    svc.run_cycle(w.config(), request_token="seed")
    at.sidebar.selectbox[0].set_value(w.exp).run(timeout=_RUN_TIMEOUT)
    _go(at, "Paper Trading")
    at.selectbox(key="trace_cycle").set_value(svc.inspect(w.exp)["cycles"][0].cycle_id).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert any("Paper fill (simulated)" in e.label for e in at.expander)


def _open_trace(at, svc, w):
    at.sidebar.selectbox[0].set_value(w.exp).run(timeout=_RUN_TIMEOUT)
    _go(at, "Paper Trading")
    cid = svc.inspect(w.exp)["cycles"][0].cycle_id
    at.selectbox(key="trace_cycle").set_value(cid).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    return cid


def _box_text(at):
    return " ".join(x.value for x in list(at.success) + list(at.error) + list(at.warning) + list(at.info))


def test_trace_shows_a_completed_linked_monitoring_run_with_its_findings(paper_world, workdir):
    from tests.test_appsvc import _monitoring_world
    from tests.test_paper_runtime import ScriptedStrategy
    w, svc, at = paper_world
    ScriptedStrategy.SCRIPT.clear()
    svc.run_cycle(w.config(), request_token="seed", monitoring=False)
    paths, mstore = _monitoring_world(workdir / "data")                # a REAL monitoring run in the app's monitoring store
    run = mstore.list_runs()[0]
    cyc = svc.store().list_cycles(w.exp)[0]
    cyc.monitoring = {"outcome": "COMPLETED", "run_id": run.run_id, "status": "CRITICAL", "persisted": True}
    svc.store().save_cycle(cyc)
    _open_trace(at, svc, w)
    assert any(run.run_id in m.value.to_string() for m in at.dataframe)
    assert any("Monitoring completed" in x.value for x in at.success)
    assert any("Scripted problem" in x.value for x in at.markdown)       # findings of the linked run are listed
    assert not any("No monitoring" in x.value for x in at.markdown)


def test_trace_distinguishes_failed_pending_skipped_not_configured_and_missing_monitoring(paper_world):
    from tests.test_paper_runtime import ScriptedStrategy
    w, svc, at = paper_world
    ScriptedStrategy.SCRIPT.clear()
    svc.run_cycle(w.config(), request_token="seed", monitoring=False)
    cases = {
        "FAILED": ({"outcome": "FAILED", "status": "ERROR", "error": "monitoring database is locked"}, "locked", True),
        "PENDING": ({"outcome": "PENDING", "status": "PENDING"}, "pending", True),
        "SKIPPED": ({"outcome": "SKIPPED", "reason": "policy: x"}, "skipped", False),
        "NOT_CONFIGURED": ({"outcome": "NOT_CONFIGURED", "reason": "none"}, "not configured", True),
        "NOT_RECORDED": (None, "No monitoring outcome was recorded", True),
    }
    for state, (mon, phrase, can_run) in cases.items():
        cyc = svc.store().list_cycles(w.exp)[0]
        cyc.monitoring = mon
        svc.store().save_cycle(cyc)
        at2 = AppTest.from_file(APP_PATH)
        at2.session_state["_paper_service"] = svc
        at2.run(timeout=_RUN_TIMEOUT)
        _open_trace(at2, svc, w)
        text = _box_text(at2)
        assert f"{state}:" in text and phrase.lower() in text.lower(), (state, text[-400:])
        assert any(b.label == "Run monitoring for this cycle" for b in at2.button) is can_run
        assert "COMPLETED:" not in text


def test_monitoring_only_button_runs_monitoring_once_without_a_new_cycle_order_or_fill(paper_world):
    from tests.test_paper_runtime import ScriptedStrategy
    w, svc, at = paper_world
    ScriptedStrategy.SCRIPT.clear()
    svc.run_cycle(w.config(), request_token="seed", monitoring=False)
    cyc = svc.store().list_cycles(w.exp)[0]
    cyc.monitoring = None                                                # the historical, unlinked shape
    svc.store().save_cycle(cyc)
    cid = _open_trace(at, svc, w)
    assert w.monitor_calls == []
    _button(at, "Run monitoring for this cycle").click().run(timeout=_RUN_TIMEOUT)      # unconfirmed
    assert w.monitor_calls == [] and any("Tick the confirmation" in x.value for x in at.warning)
    _checkbox(at, "I confirm: run monitoring only for this cycle").check()
    _button(at, "Run monitoring for this cycle").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert len(w.monitor_calls) == 1
    stored = svc.store().get_cycle(cid)
    assert stored.monitoring["outcome"] == "COMPLETED" and stored.monitoring["late"] is True
    assert len(svc.store().list_cycles(w.exp)) == 1 and svc.store().list_fills(w.exp) == [] and svc.store().list_intents(w.exp) == []
    at.run(timeout=_RUN_TIMEOUT); at.run(timeout=_RUN_TIMEOUT)
    assert len(w.monitor_calls) == 1                                    # reruns never repeat it


def test_run_result_view_shows_the_monitoring_outcome_of_a_no_action_cycle(paper_world):
    from tests.test_paper_runtime import ScriptedStrategy
    w, svc, at = paper_world
    ScriptedStrategy.SCRIPT.clear()
    _go(at, "Paper Trading")
    _select_all(at, w)
    _run(at)
    assert len(w.monitor_calls) == 1
    assert any(e.label == "Monitoring" for e in at.expander)
    assert "No monitoring run is linked" not in " ".join(x.value for x in at.markdown)


def test_paper_page_cannot_offer_a_live_mode_or_an_unregistered_strategy(workdir):
    _populate_one_experiment()
    at = _go(AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT), "Paper Trading")
    labels = " ".join(str(getattr(w, "label", "")) for w in list(at.selectbox) + list(at.text_input))
    assert "live" not in labels.lower() and "broker" not in labels.lower()
    strategy_options = at.selectbox(key="paper_strategy").options
    assert "ema_cross" in strategy_options and "candidate" not in " ".join(strategy_options).lower()


# ---------------------------------------------------------------------------
# Monitoring & alerts: viewing sends nothing and runs nothing
# ---------------------------------------------------------------------------


def test_monitoring_page_shows_persisted_findings_and_alert_queue(workdir):
    from tests.test_appsvc import _monitoring_world
    _monitoring_world(workdir / "data")
    at = _go(AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT), "Monitoring & Alerts")
    assert any("Scripted problem" in df.value.to_string() for df in at.dataframe)
    assert {"Pending", "Failed", "Delivered"} <= {m.label for m in at.metric}
    counts = __import__("src.appsvc.monitoring", fromlist=["x"]).alert_counts(AppPaths())
    assert counts["PENDING"] == 1 and counts["DELIVERED"] == 0


def test_dispatch_without_confirmation_sends_nothing(workdir):
    from tests.test_appsvc import _monitoring_world
    _monitoring_world(workdir / "data")
    at = _go(AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT), "Monitoring & Alerts")
    _button(at, "Dispatch pending alerts now").click().run(timeout=_RUN_TIMEOUT)
    assert any("Tick the confirmation" in w.value for w in at.warning)
    assert __import__("src.appsvc.monitoring", fromlist=["x"]).alert_counts(AppPaths())["DELIVERED"] == 0


def test_opening_the_app_never_calls_the_llm_or_a_live_broker(workdir, monkeypatch):
    import sys
    before = {m for m in sys.modules if m.split(".")[0] in {"anthropic"} or m.startswith("src.broker")}
    at = AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT)
    for page in PAGES:
        _go(at, page)
    after = {m for m in sys.modules if m.split(".")[0] in {"anthropic"} or m.startswith("src.broker")}
    assert after == before


# ---------------------------------------------------------------------------
# Sprint 20: Home, persisted account, approval queue, activity
# ---------------------------------------------------------------------------


def test_home_is_read_only_and_requests_nothing(paper_world, monkeypatch):
    w, svc, at = paper_world
    calls = []
    for name in ("preflight", "run_cycle", "run_with_preflight", "approve_proposal", "reject_proposal", "monitor_cycle", "create_paper_account"):
        real = getattr(svc, name)
        monkeypatch.setattr(svc, name, lambda *a, _n=name, _r=real, **k: (calls.append(_n), _r(*a, **k))[1])
    before = (len(w.store().list_cycles(w.exp)), len(w.provider.calls) if hasattr(w.provider, "calls") else None)
    at.sidebar.selectbox(key="account_id").set_value(w.exp).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    at.run(timeout=_RUN_TIMEOUT); at.run(timeout=_RUN_TIMEOUT)
    assert calls == [] and len(w.store().list_cycles(w.exp)) == before[0]
    if before[1] is not None:
        assert len(w.provider.calls) == before[1]


def test_selecting_an_account_persists_it_and_a_new_session_restores_it(paper_world, workdir):
    import json
    w, svc, at = paper_world
    assert not AppPaths().prefs_file.exists()                             # nothing is remembered until an explicit choice
    snapshot = (w.reg_path.read_bytes(), w.store_path.read_bytes())
    at.sidebar.selectbox(key="account_id").set_value(w.exp).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert json.loads(AppPaths().prefs_file.read_text())["last_account_id"] == w.exp
    assert (w.reg_path.read_bytes(), w.store_path.read_bytes()) == snapshot   # selection mutated no portfolio or ledger
    at2 = AppTest.from_file(APP_PATH)                                     # restart/refresh
    at2.session_state["_paper_service"] = svc
    at2.run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at2)
    assert at2.sidebar.selectbox(key="account_id").value == w.exp
    assert w.store().list_cycles(w.exp) == []                             # restoring executed nothing


def test_a_missing_or_malformed_preference_warns_and_is_never_replaced_silently(paper_world, workdir):
    from src.appsvc import prefs
    w, svc, _ = paper_world
    prefs.save_account(AppPaths(), 999)
    at = AppTest.from_file(APP_PATH)
    at.session_state["_paper_service"] = svc
    at.run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert at.sidebar.selectbox(key="account_id").value is None
    assert any("no longer available" in x.value for x in at.sidebar.warning)
    AppPaths().prefs_file.write_text("{broken")
    at2 = AppTest.from_file(APP_PATH)
    at2.session_state["_paper_service"] = svc
    at2.run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at2)
    assert at2.sidebar.selectbox(key="account_id").value is None
    assert any("could not be read" in x.value for x in at2.sidebar.warning)
    assert AppPaths().prefs_file.read_text() == "{broken"                 # not repaired, not overwritten by viewing


def test_home_shows_the_pending_approval_and_its_primary_action_navigates_without_running(paper_world):
    w, svc, at = paper_world
    svc.run_with_preflight(w.config(), request_token="seed")
    at.sidebar.selectbox(key="account_id").set_value(w.exp).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert any("Review proposed trade" in x.value for x in list(at.warning) + list(at.error) + list(at.info))
    n = len(w.store().list_cycles(w.exp))
    at.button(key="home_primary").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert at.sidebar.radio(key="page").value == "Paper Trading"
    assert len(w.store().list_cycles(w.exp)) == n and w.store().list_fills(w.exp) == []


def test_optional_features_off_do_not_make_home_look_unhealthy(paper_world):
    w, svc, at = paper_world
    at.sidebar.selectbox(key="account_id").set_value(w.exp).run(timeout=_RUN_TIMEOUT)
    assert not any("optional" in x.value.lower() for x in at.error)
    assert any("Optional items never make the app unhealthy" in c.value for c in at.caption)


def test_activity_page_links_records_only_through_stored_ids(paper_world):
    w, svc, at = paper_world
    svc.run_with_preflight(w.config(), request_token="seed")
    pid = _pending(svc, w)[0]["proposal"].proposal_id
    svc.approve_proposal(pid, w.exp)
    at.sidebar.selectbox(key="account_id").set_value(w.exp).run(timeout=_RUN_TIMEOUT)
    _go(at, "Activity")
    frame = at.dataframe[0].value
    assert {"Cycle", "Proposal", "Order", "Fill"} <= set(frame["Type"])
    fill_row = frame[frame["Type"] == "Fill"].iloc[0]
    assert "client_order_id=" in fill_row["Linked ids"] and "cycle_id" not in fill_row["Linked ids"]
    at.multiselect(key="act_cats").set_value(["Fill"]).run(timeout=_RUN_TIMEOUT)
    assert set(at.dataframe[0].value["Type"]) == {"Fill"}


# ---------------------------------------------------------------------------
# Sprint 21: instrument selector (presets + Custom…)
# ---------------------------------------------------------------------------


def test_symbol_dropdown_lists_the_shared_presets_with_custom_last(paper_world):
    from src.appsvc import instruments
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    sel = at.selectbox(key="paper_symbol_choice")
    assert list(sel.options)[-1] == instruments.CUSTOM and sel.value is None       # nothing silently preselected
    shown = " ".join(sel.options)
    assert all(sym in shown for sym in ("QQQ", "SPY", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMD"))
    assert not any(t.key == "paper_symbol_custom" for t in at.text_input)             # hidden until Custom…


def test_custom_reveals_the_field_normalises_and_rejects_bad_input_without_substituting(paper_world):
    from src.appsvc import instruments
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    at.selectbox(key="paper_symbol_choice").set_value(instruments.CUSTOM).run(timeout=_RUN_TIMEOUT)
    assert any(t.key == "paper_symbol_custom" for t in at.text_input)
    at.text_input(key="paper_symbol_custom").set_value("  tsla ").run(timeout=_RUN_TIMEOUT)
    assert any("TSLA" in c.value and "unverified" in c.value for c in at.caption)
    for bad in ("", "   ", "TS LA", "T$LA", "A" * 30):
        at.text_input(key="paper_symbol_custom").set_value(bad).run(timeout=_RUN_TIMEOUT)
        _assert_no_exception(at)
        assert not any(b.label == "Run paper cycle" for b in at.button), bad          # no config: nothing can run
    assert not AppPaths().prefs_file.exists() or "TS LA" not in AppPaths().prefs_file.read_text()


def test_the_entered_custom_value_survives_switching_to_a_preset_and_back(paper_world):
    from src.appsvc import instruments
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    at.selectbox(key="paper_symbol_choice").set_value(instruments.CUSTOM).run(timeout=_RUN_TIMEOUT)
    at.text_input(key="paper_symbol_custom").set_value("tsla").run(timeout=_RUN_TIMEOUT)
    at.selectbox(key="paper_symbol_choice").set_value("SPY").run(timeout=_RUN_TIMEOUT)
    at.selectbox(key="paper_symbol_choice").set_value(instruments.CUSTOM).run(timeout=_RUN_TIMEOUT)
    assert at.text_input(key="paper_symbol_custom").value == "tsla"


def test_changing_the_symbol_runs_nothing_and_orders_nothing(paper_world, monkeypatch):
    w, svc, at = paper_world
    calls = []
    for name in ("preflight", "run_cycle", "run_with_preflight", "approve_proposal"):
        real = getattr(svc, name)
        monkeypatch.setattr(svc, name, lambda *a, _n=name, _r=real, **k: (calls.append(_n), _r(*a, **k))[1])
    _go(at, "Paper Trading")
    _select_all(at, w)
    for sym in ("SPY", "AAPL", "TSLA", "QQQ"):
        _pick_symbol(at, sym)
    at.run(timeout=_RUN_TIMEOUT)
    assert calls == [] and w.store().list_cycles(w.exp) == [] and w.store().list_intents(w.exp) == [] and w.store().list_fills(w.exp) == []


def test_the_symbol_is_remembered_independently_of_the_account_and_restoring_it_mutates_nothing(paper_world):
    import json
    from src.appsvc import prefs
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    _pick_symbol(at, "SPY")
    at.sidebar.selectbox(key="account_id").set_value(w.exp).run(timeout=_RUN_TIMEOUT)
    data = json.loads(AppPaths().prefs_file.read_text())
    assert data["last_symbol"] == "SPY" and data["last_account_id"] == w.exp          # both kept, neither overwrote the other
    snapshot = (w.reg_path.read_bytes(), w.store_path.read_bytes())
    at2 = AppTest.from_file(APP_PATH)
    at2.session_state["_paper_service"] = svc
    at2.run(timeout=_RUN_TIMEOUT)
    _go(at2, "Paper Trading")
    assert at2.selectbox(key="paper_symbol_choice").value == "SPY"
    assert (w.reg_path.read_bytes(), w.store_path.read_bytes()) == snapshot and w.store().list_cycles(w.exp) == []


def test_a_saved_custom_symbol_is_restored_visibly_and_not_trusted(paper_world):
    from src.appsvc import instruments, prefs
    w, svc, _ = paper_world
    prefs.save_symbol(AppPaths(), "ZZZZ")
    at = AppTest.from_file(APP_PATH)
    at.session_state["_paper_service"] = svc
    at.run(timeout=_RUN_TIMEOUT)
    _go(at, "Paper Trading")
    assert at.selectbox(key="paper_symbol_choice").value == instruments.CUSTOM
    assert at.text_input(key="paper_symbol_custom").value == "ZZZZ"
    assert any("revalidated" in c.value for c in at.caption)
    assert w.store().list_cycles(w.exp) == []


def test_a_custom_symbol_flows_through_preflight_and_the_cycle_identity(paper_world):
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    _select_all(at, w, symbol="TSLA")
    _run(at)
    cyc = w.store().list_cycles(w.exp)[0]
    assert cyc.symbol == "TSLA"


def test_a_pending_proposal_for_another_symbol_blocks_run_and_keeps_its_identity(paper_world):
    w, svc, at = paper_world
    svc.run_with_preflight(w.config(), request_token="seed")                         # QQQ proposal
    pid = _pending(svc, w)[0]["proposal"].proposal_id
    _go(at, "Paper Trading")
    _select_all(at, w, symbol="SPY")
    assert _button(at, "Run paper cycle").disabled is True
    assert any("pending proposal exists for QQQ" in x.value for x in at.warning)
    assert _pending(svc, w)[0]["proposal"].proposal_id == pid and _pending(svc, w)[0]["proposal"].symbol == "QQQ"


# ---------------------------------------------------------------------------
# Sprint 21 (ADR-0058): Opportunity Scanner page. Network-free: the scanner service is injected with a synthetic provider and
# a fixed clock. These AppTests need the real Streamlit runtime.
# ---------------------------------------------------------------------------


@pytest.fixture
def scanner_world(workdir, monkeypatch):
    from datetime import datetime as _dt, timezone as _tz
    from src.appsvc.scanner import ScannerService
    from tests.test_scanner import ScanProvider
    md = MarketDataService(provider=ScanProvider(), cache_dir=workdir / "cache", use_cache=False)

    def _service(paths):
        return ScannerService(paths, market_data=md, clock=lambda: _dt(2026, 10, 7, 22, 0, tzinfo=_tz.utc))

    monkeypatch.setattr("src.dashboard.shell.scanner_service", _service)
    return workdir


def test_scanner_page_is_read_only_on_open_and_offers_the_starter_list(scanner_world):
    at = AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT)
    _go(at, "Opportunity Scanner")
    assert any(b.label == "Create the US Liquid Starter watchlist" for b in at.button)
    assert any("READ-ONLY" in w.value for w in at.warning)
    assert not (scanner_world / "data" / "scanner").exists()


def test_home_links_to_the_scanner_without_running_anything(scanner_world):
    at = AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT)
    _button(at, "Open the Opportunity Scanner").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert at.sidebar.radio(key="page").value == "Opportunity Scanner"
    assert not (scanner_world / "data" / "scanner").exists()


def test_scanner_page_seeds_scans_and_creates_no_paper_records(scanner_world):
    at = AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT)
    _go(at, "Opportunity Scanner")
    _button(at, "Create the US Liquid Starter watchlist").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    at.selectbox(key="scanner_wl").select_index(0).run(timeout=_RUN_TIMEOUT)
    at.selectbox(key="scanner_strategy").select("ema_cross").run(timeout=_RUN_TIMEOUT)
    _button(at, "Scan 8 symbols").click().run(timeout=_RUN_TIMEOUT * 4)
    _assert_no_exception(at)
    assert any("Completed" in x.value for x in list(at.success) + list(at.warning) + list(at.info) + list(at.error))
    assert (scanner_world / "data" / "scanner" / "scanner.db").exists()
    assert not (scanner_world / "data" / "paper").exists() and not (scanner_world / "data" / "monitoring").exists()


def test_scanner_rejects_an_invalid_watchlist_without_saving(scanner_world):
    at = AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT)
    _go(at, "Opportunity Scanner")
    at.text_input(key="scanner_new_name").set_value("bad")
    at.text_area(key="scanner_new_syms").set_value("AAPL bad$sym")
    next(b for b in at.button if b.label == "Create watchlist").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert any("not accepted" in e.value for e in at.error)
    assert not (scanner_world / "data" / "scanner" / "scanner.db").exists()
