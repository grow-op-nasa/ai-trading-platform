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


def test_overview_with_no_registry_explains_the_next_step(workdir):
    at = AppTest.from_file(APP_PATH).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert any("No experiment registry exists yet" in i.value for i in at.info)


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


def _select_all(at, w, symbol="QQQ"):
    """Operate the visible selectors exactly as a user does (no profile, no service-side setup)."""
    from tests.test_paper_runtime import STRAT
    at.selectbox(key="paper_strategy").set_value(STRAT).run(timeout=_RUN_TIMEOUT)
    at.selectbox(key="paper_account").set_value(w.exp).run(timeout=_RUN_TIMEOUT)
    at.text_input(key="paper_symbol_text").set_value(symbol).run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)


def _confirm_and_run(at):
    _checkbox(at, "I confirm: run one paper-only cycle now").check()
    _button(at, "Run one paper cycle").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)


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
    assert at.text_input(key="paper_symbol_text") is not None
    assert not any(b.label == "Run preflight" for b in at.button)       # only offered once the selections are complete
    _select_all(at, w)
    assert any(b.label == "Run preflight" for b in at.button)
    assert _button(at, "Run one paper cycle").disabled is True


def test_ema_cross_is_offered_only_because_it_is_registered():
    import src.strategies  # noqa: F401
    from src.appsvc.strategies import eligible_names
    from src.strategies.registry import available_strategies
    assert set(eligible_names(AppPaths())) == set(available_strategies())


def test_preflight_runs_only_on_an_explicit_click(paper_world, monkeypatch):
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


def test_run_is_disabled_until_a_preflight_passes(paper_world):
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    _select_all(at, w)
    assert _button(at, "Run one paper cycle").disabled is True
    assert w.store().list_cycles(w.exp) == []


def test_changing_any_selection_invalidates_the_preflight(paper_world):
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    _select_all(at, w)
    at.button(key="btn_preflight").click().run(timeout=_RUN_TIMEOUT)
    assert _button(at, "Run one paper cycle").disabled is False
    at.text_input(key="paper_symbol_text").set_value("SPY").run(timeout=_RUN_TIMEOUT)
    assert _button(at, "Run one paper cycle").disabled is True
    assert any("no longer applies" in x.value for x in at.warning)
    at.text_input(key="paper_symbol_text").set_value("QQQ").run(timeout=_RUN_TIMEOUT)
    assert _button(at, "Run one paper cycle").disabled is False        # same configuration again: the earlier approval applies
    at.number_input(key="paper_slip").set_value(7.0).run(timeout=_RUN_TIMEOUT)    # a settings change also invalidates
    assert _button(at, "Run one paper cycle").disabled is True


def test_failed_preflight_blocks_execution_with_an_actionable_explanation(paper_world):
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    class _Down:
        def get_candles(self, *a, **k):
            raise NoDataError("provider unavailable (test)")
        fetch_candles = get_history = get_dataset = get_candles
    svc._market_data = _Down()                                         # market data cannot be obtained
    _select_all(at, w)
    at.button(key="btn_preflight").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert _button(at, "Run one paper cycle").disabled is True
    assert any("market_data" in m.value for m in at.markdown)           # the failing check is named with its remediation
    assert w.store().list_cycles(w.exp) == []


def test_one_explicit_run_executes_one_cycle_and_reruns_never_repeat_it(paper_world):
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    _select_all(at, w)
    at.button(key="btn_preflight").click().run(timeout=_RUN_TIMEOUT)
    assert any(b.label == "Run one paper cycle" and not b.disabled for b in at.button)
    _confirm_and_run(at)
    assert len(w.store().list_cycles(w.exp)) == 1 and len(w.store().list_fills(w.exp)) == 1
    assert any("ORDER_FILLED" in s.value for s in at.success)
    labels = [e.label for e in at.expander]
    assert "Signal at the decision bar" in labels and "Risk decision" in labels and "Paper fill (simulated)" in labels
    at.run(timeout=_RUN_TIMEOUT); at.run(timeout=_RUN_TIMEOUT)
    assert len(w.store().list_cycles(w.exp)) == 1 and len(w.store().list_fills(w.exp)) == 1 and len(w.monitor_calls) == 1
    # a brand-new session (browser refresh) re-runs nothing and shows the persisted cycle in history
    at2 = AppTest.from_file(APP_PATH)
    at2.session_state["_paper_service"] = svc
    at2.run(timeout=_RUN_TIMEOUT)
    _go(at2, "Paper Trading")
    assert len(w.store().list_cycles(w.exp)) == 1 and len(w.store().list_fills(w.exp)) == 1


def test_the_service_boundary_refuses_a_run_whose_approval_does_not_match(paper_world):
    w, svc, at = paper_world
    out = svc.run_cycle(w.config(), approved_config_hash="0" * 16)
    assert out.refused and "does not match" in out.refused
    assert w.store() is None or w.store().list_cycles(w.exp) == []


def test_a_click_without_confirmation_runs_nothing(paper_world):
    w, svc, at = paper_world
    _go(at, "Paper Trading")
    _select_all(at, w)
    at.button(key="btn_preflight").click().run(timeout=_RUN_TIMEOUT)
    _button(at, "Run one paper cycle").click().run(timeout=_RUN_TIMEOUT)
    _assert_no_exception(at)
    assert w.store().list_cycles(w.exp) == [] and any("Tick the confirmation" in x.value for x in at.warning)


def test_no_signal_is_shown_as_a_valid_outcome_not_a_failure(paper_world):
    w, svc, at = paper_world
    from tests.test_paper_runtime import ScriptedStrategy
    ScriptedStrategy.SCRIPT.clear()                # no actionable signal on the decision bar
    _go(at, "Paper Trading")
    _select_all(at, w)
    at.button(key="btn_preflight").click().run(timeout=_RUN_TIMEOUT)
    _confirm_and_run(at)
    assert any("NO_ACTION" in i.value for i in at.info) and not at.error
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
