"""Smoke tests for `src.dashboard` -- Sprint 9 (`DECISIONS.md`,
ADR-0042).

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

from streamlit.testing.v1 import AppTest  # noqa: E402

from scripts.run_experiment import run_experiment  # noqa: E402
from src.data.exceptions import NoDataError  # noqa: E402
from src.data.service import MarketDataService  # noqa: E402
from src.experiments.registry import ExperimentRegistry  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parent.parent / "src" / "dashboard" / "app.py")

EMA_CLOSES = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120, 110, 100, 90, 80]
RSI_CLOSES = [100 - i * 2 for i in range(15)] + [70 + i * 3 for i in range(15)]


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
    without touching the real project's `data/experiments.db`."""
    monkeypatch.chdir(tmp_path)
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


def test_app_runs_with_no_experiments(workdir):
    at = AppTest.from_file(APP_PATH).run()
    _assert_no_exception(at)
    assert any("No experiments found" in info.value for info in at.info)


def test_app_runs_overview_page_by_default(workdir):
    _populate_one_experiment()
    at = AppTest.from_file(APP_PATH).run()
    _assert_no_exception(at)
    assert any("Overview" in h.value for h in at.header)


def test_app_runs_experiment_analysis_page(workdir):
    run_result = _populate_one_experiment()
    at = AppTest.from_file(APP_PATH).run()
    at.sidebar.radio[0].set_value("Backtest / Experiment Analysis").run()
    _assert_no_exception(at)
    assert any(str(run_result.experiment_id) in h.value for h in at.header)
    # Metrics rendered as st.metric widgets -- at least the core four.
    metric_labels = {m.label for m in at.metric}
    assert {"Total return", "Sharpe ratio", "Max drawdown", "Win rate"} <= metric_labels


def test_app_runs_comparison_page_with_two_experiments(workdir):
    run_a = _populate_one_experiment(symbol="SPY", strategy="ema_cross")
    run_b = _populate_one_experiment(
        symbol="QQQ", strategy="rsi_mean_reversion", params={"period": 5}, closes=RSI_CLOSES
    )
    at = AppTest.from_file(APP_PATH).run()
    at.sidebar.radio[0].set_value("Strategy Comparison").run()
    at.sidebar.multiselect[0].set_value([run_a.experiment_id, run_b.experiment_id]).run()
    _assert_no_exception(at)
    assert any("Comparison" in h.value for h in at.header)
    # Differing symbol/strategy should surface at least one warning.
    assert len(at.warning) >= 1


def test_app_runs_comparison_page_with_fewer_than_two_selected(workdir):
    _populate_one_experiment()
    at = AppTest.from_file(APP_PATH).run()
    at.sidebar.radio[0].set_value("Strategy Comparison").run()
    _assert_no_exception(at)
    assert any("two or more" in i.value for i in at.info)


def test_app_runs_paper_portfolio_page_with_an_open_position(workdir):
    # rsi_mean_reversion against RSI_CLOSES leaves an open position at
    # the end of the run (proven already by
    # tests/test_run_experiment_script.py's sibling assertions) -- a
    # real exercise of the priced-position rendering path.
    run_result = _populate_one_experiment(
        symbol="QQQ", strategy="rsi_mean_reversion", params={"period": 5}, closes=RSI_CLOSES
    )
    at = AppTest.from_file(APP_PATH).run()
    at.sidebar.radio[0].set_value("Paper Portfolio").run()
    _assert_no_exception(at)
    assert any(str(run_result.experiment_id) in h.value for h in at.header)
    metric_labels = {m.label for m in at.metric}
    assert {"Cash", "Equity", "Unrealized P&L", "Realized P&L"} <= metric_labels


def test_paper_portfolio_page_degrades_gracefully_when_price_unavailable(workdir, monkeypatch):
    run_result = _populate_one_experiment(
        symbol="QQQ", strategy="rsi_mean_reversion", params={"period": 5}, closes=RSI_CLOSES
    )

    def _raise_no_data(self, symbol, period="5d", interval="1d"):
        raise NoDataError("no data available")

    monkeypatch.setattr(MarketDataService, "get_history", _raise_no_data)

    at = AppTest.from_file(APP_PATH).run()
    at.sidebar.radio[0].set_value("Paper Portfolio").run()
    # A missing market price must degrade to a warning + "N/A", never a
    # raw traceback shown to the user (Sprint 9 spec's error-handling
    # requirement).
    _assert_no_exception(at)
    assert len(at.warning) >= 1


# ---------------------------------------------------------------------------
# Read-only: rendering never mutates the underlying Portfolio/Experiment
# ---------------------------------------------------------------------------


def test_rendering_the_dashboard_never_mutates_the_saved_portfolio(workdir):
    run_result = _populate_one_experiment(
        symbol="QQQ", strategy="rsi_mean_reversion", params={"period": 5}, closes=RSI_CLOSES
    )
    registry = ExperimentRegistry()
    before = registry.get_portfolio(run_result.experiment_id)
    before_cash = before.cash
    before_open = {p.symbol: p.quantity for p in before.positions.values()}

    at = AppTest.from_file(APP_PATH).run()
    at.sidebar.radio[0].set_value("Paper Portfolio").run()
    at.sidebar.radio[0].set_value("Overview").run()

    after = registry.get_portfolio(run_result.experiment_id)
    assert after.cash == before_cash
    assert {p.symbol: p.quantity for p in after.positions.values()} == before_open


def test_rendering_the_dashboard_makes_no_network_call_when_no_positions_are_open(workdir, monkeypatch):
    # ema_cross against EMA_CLOSES closes every position it opens
    # before the run ends (proven by test_run_experiment_script.py) --
    # in that case PortfolioValuationService never calls price_lookup
    # at all, so MarketDataService.get_history must never be invoked.
    calls = []

    def _tracking_get_history(self, symbol, period="5d", interval="1d"):
        calls.append(symbol)
        return pd.DataFrame({"close": [1.0]})

    monkeypatch.setattr(MarketDataService, "get_history", _tracking_get_history)

    run_result = _populate_one_experiment()  # ema_cross / EMA_CLOSES
    registry = ExperimentRegistry()
    saved_portfolio = registry.get_portfolio(run_result.experiment_id)
    assert len(saved_portfolio.positions) == 0  # sanity check on the fixture

    at = AppTest.from_file(APP_PATH).run()
    at.sidebar.radio[0].set_value("Paper Portfolio").run()
    _assert_no_exception(at)
    assert calls == []
