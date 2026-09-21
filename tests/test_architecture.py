"""Tests that protect the architecture itself, not just implementation
details -- dependency direction, data lineage, and documented
limitations that must not be silently forgotten (architecture review
cleanup, `DECISIONS.md`, ADR-0031 through ADR-0034).

These are deliberately static/structural where possible (reading source
files, checking for the absence of a method) rather than behavioral,
since the property being protected is architectural shape, not a
runtime calculation.
"""

from __future__ import annotations

import pathlib
import re

import pandas as pd
import pytest

from src.execution.engine import PaperBroker
from src.experiments.registry import ExperimentRegistry
from src.portfolio.models import AccountState
from src.risk import engine as risk_engine
from src.risk import models as risk_models
from src.signals.models import Signal, SignalDirection
from src.strategies.ema_cross import EMACrossStrategy
from src.backtesting.engine import Backtester

SRC_ROOT = pathlib.Path(__file__).resolve().parent.parent / "src"

_IMPORT_PATTERN = re.compile(r"^\s*(?:from|import)\s+src\.risk\b", re.MULTILINE)


# ---------------------------------------------------------------------------
# Dependency direction (DECISIONS.md, ADR-0031): broker -> portfolio,
# risk -> portfolio, execution -> portfolio -- never broker -> risk.
# ---------------------------------------------------------------------------


def _broker_source_files() -> list[pathlib.Path]:
    return sorted((SRC_ROOT / "broker").glob("*.py"))


def test_broker_modules_do_not_import_src_risk():
    offenders = {
        path.name: path.read_text()
        for path in _broker_source_files()
        if _IMPORT_PATTERN.search(path.read_text())
    }
    assert offenders == {}, (
        f"src/broker must not depend on src/risk -- broker is foundational "
        f"connectivity infrastructure, risk consumes account state, not the "
        f"other way around (DECISIONS.md, ADR-0031). Offending files: "
        f"{list(offenders)}"
    )


def test_risk_modules_do_not_import_src_broker_or_src_execution():
    # Sprint 7 (DECISIONS.md, ADR-0039): PortfolioRiskEngine consumes the
    # neutral Portfolio/Signal models to make a decision, then hands a
    # structured RiskDecision back to the caller -- it must never reach
    # into src.broker or src.execution itself. The dependency direction
    # is strictly Portfolio -> Risk -> Execution -> Broker; risk depends
    # on portfolio and signals only.
    import_pattern = re.compile(r"^\s*(?:from|import)\s+src\.(broker|execution)\b", re.MULTILINE)
    for path in (SRC_ROOT / "risk").glob("*.py"):
        text = path.read_text()
        match = import_pattern.search(text)
        assert match is None, (
            f"{path.name} must not depend on src.broker or src.execution -- "
            f"risk decides, execution/broker act on that decision, not the "
            f"other way around (DECISIONS.md, ADR-0039). Found an import of "
            f"src.{match.group(1) if match else ''}."
        )


def test_execution_does_not_duplicate_risk_sizing_logic():
    # Sprint 7 cleanup (DECISIONS.md, ADR-0040): there must be exactly
    # one authoritative implementation of each risk rule. PaperBroker
    # consumes an already-computed SizingDecision/RiskDecision's
    # quantity directly (src/execution/engine.py's own submit_signal()/
    # _build_order()) -- it must never independently recalculate a risk
    # percentage, a stop-distance quantity, or a portfolio-exposure
    # ceiling.
    #
    # Checks actual *import* statements, not prose -- src/execution's
    # own docstrings legitimately *mention* PortfolioRiskEngine/
    # PortfolioRiskLimits to explain how a RiskDecision reaches
    # execution (e.g. portfolio_sync.py's stop_price parameter doc),
    # without importing or reimplementing them. Importing the
    # computational risk symbols themselves (as opposed to the plain
    # data shape `SizingDecision`, which execution has always legitimately
    # depended on, DECISIONS.md ADR-0022) would be the real red flag:
    # it would mean execution has its own way to *compute* a risk
    # decision, not just consume one.
    forbidden_imports = re.compile(
        r"^\s*(?:from\s+src\.risk(?:\.\w+)?\s+import\s+.*\b"
        r"(PortfolioRiskEngine|PortfolioRiskLimits|RiskLimits)\b"
        r"|from\s+src\.risk\.portfolio_risk\s+import)",
        re.MULTILINE,
    )
    offenders = [
        path.name
        for path in (SRC_ROOT / "execution").glob("*.py")
        if forbidden_imports.search(path.read_text())
    ]
    assert offenders == [], (
        f"src/execution must not import the risk-computation symbols "
        f"(PortfolioRiskEngine, PortfolioRiskLimits, RiskLimits) or "
        f"anything from src.risk.portfolio_risk -- it consumes an "
        f"already-decided quantity via the plain SizingDecision shape, "
        f"never recomputes one itself (DECISIONS.md, ADR-0040). "
        f"Offending files: {offenders}"
    )


def test_portfolio_package_depends_on_nothing_else_in_this_codebase():
    # Checks actual import statements, not prose -- src/portfolio's own
    # docstrings legitimately *mention* src.risk/src.broker/src.execution
    # to explain why AccountState moved here, without importing them.
    import_pattern = re.compile(r"^\s*(?:from|import)\s+src\.(broker|risk|execution)\b", re.MULTILINE)
    for path in (SRC_ROOT / "portfolio").glob("*.py"):
        text = path.read_text()
        match = import_pattern.search(text)
        assert match is None, (
            f"{path.name} must stay a neutral domain package -- it depends "
            f"on none of broker/risk/execution, they depend on it "
            f"(DECISIONS.md, ADR-0031). Found an import of src.{match.group(1) if match else ''}."
        )


def test_account_state_no_longer_defined_in_risk_models():
    # The class itself moved, not just its import path -- a re-export
    # from src.risk is fine, but src.risk.models must not define it.
    assert not hasattr(risk_models, "AccountState")
    source = (SRC_ROOT / "risk" / "models.py").read_text()
    assert "class AccountState" not in source


def test_risk_engine_consumes_the_neutral_portfolio_account_state():
    # PositionSizer.size()'s account parameter is the same AccountState
    # class as src.portfolio.models.AccountState -- risk consumes the
    # neutral model, it doesn't define its own.
    account = AccountState(equity=10_000)
    signal = Signal(
        timestamp=pd.Timestamp("2024-01-01"),
        symbol="SPY",
        direction=SignalDirection.LONG,
        confidence=0.8,
    )
    decision = risk_engine.PositionSizer().size(signal, account, price=100.0)
    assert decision.approved is True


# ---------------------------------------------------------------------------
# Signal.symbol is required, first-class, and survives every layer
# (DECISIONS.md, ADR-0033).
# ---------------------------------------------------------------------------


def test_signal_construction_requires_symbol():
    with pytest.raises(TypeError):
        Signal(  # type: ignore[call-arg]
            timestamp=pd.Timestamp("2024-01-01"),
            direction=SignalDirection.LONG,
            confidence=0.8,
        )


def test_symbol_and_signal_id_survive_strategy_through_backtest_through_registry(tmp_path):
    # The full cross-cutting lineage claim in one test: a strategy emits
    # a Signal for a specific symbol; the Backtester passes it through
    # untouched into BacktestResult.signals; the Experiment Registry
    # persists and reconstructs it -- the same symbol and the same UUID
    # identity survive all three layers.
    closes = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120, 110, 100, 90, 80]
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    candles = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )
    strategy = EMACrossStrategy(symbol="QQQ", fast=2, slow=4)

    result = Backtester().run(strategy, candles)
    assert result.signals  # sanity check
    original = result.signals[0]
    assert original.symbol == "QQQ"

    registry = ExperimentRegistry(db_path=tmp_path / "experiments.db")
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    registry.save_signals(experiment_id, result.signals)

    fetched = registry.get_signal(original.id)

    assert fetched is not None
    assert fetched.id == original.id
    assert fetched.symbol == "QQQ"


# ---------------------------------------------------------------------------
# PaperBroker's mark-to-market limitation must stay visible, not get
# silently reintroduced as an assumed capability (DECISIONS.md, ADR-0022,
# strengthened by ADR-0031's cleanup).
# ---------------------------------------------------------------------------


def test_paper_broker_has_no_mechanism_to_mark_a_position_to_a_new_price():
    # Pins the documented limitation structurally: there is no method on
    # PaperBroker that accepts a live/current price for an open position.
    # If a future change adds real mark-to-market, it should add such a
    # method deliberately -- which will make this assertion fail and
    # force the docstrings/ADRs to be revisited, rather than the
    # capability silently appearing. See engine.py's module docstring
    # and `account_state`'s own docstring.
    broker = PaperBroker(starting_cash=100_000)
    for forbidden_method in ("mark_to_market", "update_price", "set_price", "revalue"):
        assert not hasattr(broker, forbidden_method)


def test_second_strategy_required_no_changes_to_core_pipeline_modules():
    # Proof of the closing architectural principle in Sprint 6's spec:
    # adding RSIMeanReversionStrategy (ROADMAP.md, Sprint 6 close-out)
    # must not have required touching the backtester, experiment
    # registry, attribution engine, reporter, or broker layer -- it
    # should simply plug into the existing research pipeline as a new
    # strategy file, nothing more. This checks the actual committed
    # source text of those modules for any mention of the new strategy,
    # rather than trusting a docstring's claim.
    core_modules = [
        SRC_ROOT / "backtesting",
        SRC_ROOT / "experiments" / "registry.py",
        SRC_ROOT / "attribution",
        SRC_ROOT / "research",
        SRC_ROOT / "broker",
    ]
    forbidden_mentions = ("rsi_mean_reversion", "RSIMeanReversionStrategy")

    offenders = []
    for module_path in core_modules:
        files = [module_path] if module_path.is_file() else sorted(module_path.glob("*.py"))
        for path in files:
            text = path.read_text()
            if any(mention in text for mention in forbidden_mentions):
                offenders.append(str(path))

    assert offenders == [], (
        f"Adding a second strategy must not require modifying core "
        f"pipeline modules -- it should compose without leaking "
        f"responsibilities into them. Offending files: {offenders}"
    )


# ---------------------------------------------------------------------------
# Sprint 8 (DECISIONS.md, ADR-0041): "No strategy, backtest, or
# experiment consumes raw provider data directly" -- everything passes
# through MarketDataService's validated/canonicalized dataset boundary
# first. And src/calendar is a leaf dependency: src/data depends on it,
# never the other way around.
# ---------------------------------------------------------------------------


def test_strategies_backtesting_and_experiments_never_import_the_provider_directly():
    provider_import_pattern = re.compile(
        r"^\s*(?:from\s+src\.data\.yfinance_provider\s+import|"
        r"import\s+src\.data\.yfinance_provider\b|"
        r"from\s+src\.data(?:\.\w+)?\s+import\s+.*\bYFinanceProvider\b)",
        re.MULTILINE,
    )
    consumer_packages = [
        SRC_ROOT / "strategies",
        SRC_ROOT / "backtesting",
        SRC_ROOT / "experiments",
    ]
    offenders = []
    for package in consumer_packages:
        for path in package.glob("*.py"):
            if provider_import_pattern.search(path.read_text()):
                offenders.append(str(path))

    assert offenders == [], (
        f"Strategies, the backtester, and experiments must consume candles "
        f"via MarketDataService (validated, canonicalized, session-aware), "
        f"never the raw YFinanceProvider directly (DECISIONS.md, ADR-0041's "
        f"architectural invariant). Offending files: {offenders}"
    )


def test_calendar_package_has_no_dependency_on_data_or_anything_else_in_this_codebase():
    # src/calendar is a leaf: src/data depends on it for session
    # filtering and gap detection, never the reverse (DECISIONS.md,
    # ADR-0041). Checked via actual import statements, not prose --
    # this module's own docstrings legitimately *mention* src.data to
    # explain why the package exists.
    import_pattern = re.compile(r"^\s*(?:from|import)\s+src\.(data|backtesting|strategies|experiments)\b", re.MULTILINE)
    for path in (SRC_ROOT / "calendar").glob("*.py"):
        text = path.read_text()
        match = import_pattern.search(text)
        assert match is None, (
            f"{path.name} must stay a leaf dependency -- src.calendar depends "
            f"on nothing else in this codebase, src.data depends on it "
            f"(DECISIONS.md, ADR-0041). Found an import of src.{match.group(1) if match else ''}."
        )


def test_market_data_service_is_the_only_thing_that_imports_yfinance_provider():
    # The provider is an implementation detail of src/data itself
    # (DECISIONS.md, ADR-0002/ADR-0041) -- confirms the positive half of
    # the invariant the test above checks the negative half of.
    service_source = (SRC_ROOT / "data" / "service.py").read_text()
    assert "yfinance_provider" in service_source


# ---------------------------------------------------------------------------
# Sprint 9 (DECISIONS.md, ADR-0042): "The dashboard is an interface over
# the platform; it is not the platform." Dependency direction is
# strictly Data/Experiments/Backtesting/Portfolio -> Analytics ->
# Dashboard -- analytics never depends on the dashboard or on
# Streamlit, and the dashboard never bypasses MarketDataService for a
# market price.
# ---------------------------------------------------------------------------


def test_analytics_package_does_not_depend_on_dashboard():
    # Checks actual import statements, not prose -- src/analytics's own
    # docstrings legitimately *mention* src.dashboard to explain the
    # dependency direction, without importing it.
    import_pattern = re.compile(r"^\s*(?:from|import)\s+src\.dashboard\b", re.MULTILINE)
    for path in (SRC_ROOT / "analytics").glob("*.py"):
        text = path.read_text()
        match = import_pattern.search(text)
        assert match is None, (
            f"{path.name} must not depend on src.dashboard -- the dashboard is an "
            f"interface over analytics, never the reverse (DECISIONS.md, ADR-0042). "
            f"Found an import of src.dashboard."
        )


def test_analytics_package_has_no_streamlit_dependency():
    import_pattern = re.compile(r"^\s*(?:from|import)\s+streamlit\b", re.MULTILINE)
    for path in (SRC_ROOT / "analytics").glob("*.py"):
        text = path.read_text()
        match = import_pattern.search(text)
        assert match is None, (
            f"{path.name} must not depend on streamlit -- src.analytics has to be "
            f"usable from a CLI, a test, or the dashboard equally, and must never "
            f"require Streamlit to be installed at all (DECISIONS.md, ADR-0042). "
            f"Found an import of streamlit."
        )


def test_dashboard_depends_on_analytics():
    # The positive half of the boundary: confirms src.dashboard actually
    # uses src.analytics for its computations, rather than the negative
    # check above vacuously passing because the two packages never
    # interact at all.
    import_pattern = re.compile(r"^\s*(?:from|import)\s+src\.analytics\b", re.MULTILINE)
    offending = [
        path.name
        for path in (SRC_ROOT / "dashboard").glob("*.py")
        if import_pattern.search(path.read_text())
    ]
    assert offending, (
        "src.dashboard must depend on src.analytics for its metrics/valuation -- "
        "found no file in src/dashboard importing from src.analytics at all "
        "(DECISIONS.md, ADR-0042)."
    )


def test_no_circular_dependency_between_analytics_and_dashboard():
    # Sprint 9 spec: dashboard -> analytics is required, analytics ->
    # dashboard is forbidden -- together, no cycle between the two.
    dashboard_imports_analytics = any(
        re.search(r"^\s*(?:from|import)\s+src\.analytics\b", path.read_text(), re.MULTILINE)
        for path in (SRC_ROOT / "dashboard").glob("*.py")
    )
    analytics_imports_dashboard = any(
        re.search(r"^\s*(?:from|import)\s+src\.dashboard\b", path.read_text(), re.MULTILINE)
        for path in (SRC_ROOT / "analytics").glob("*.py")
    )
    assert dashboard_imports_analytics is True
    assert analytics_imports_dashboard is False


def test_dashboard_does_not_import_yfinance():
    # Neither the raw `yfinance` package nor the internal
    # YFinanceProvider wrapper -- every market price the dashboard shows
    # must come from src.analytics.valuation.latest_price(), which goes
    # through MarketDataService (DECISIONS.md, ADR-0042, section 23).
    import_pattern = re.compile(
        r"^\s*(?:from\s+yfinance\b|import\s+yfinance\b|"
        r"from\s+src\.data\.yfinance_provider\s+import|"
        r"import\s+src\.data\.yfinance_provider\b|"
        r"from\s+src\.data(?:\.\w+)?\s+import\s+.*\bYFinanceProvider\b)",
        re.MULTILINE,
    )
    offenders = [
        path.name
        for path in (SRC_ROOT / "dashboard").glob("*.py")
        if import_pattern.search(path.read_text())
    ]
    assert offenders == [], (
        f"src/dashboard must never import yfinance (directly or via "
        f"YFinanceProvider) -- market prices must flow through "
        f"MarketDataService only (DECISIONS.md, ADR-0042). Offending files: "
        f"{offenders}"
    )


def test_dashboard_does_not_read_the_market_data_cache_directly():
    # The other half of "doesn't bypass MarketDataService": no direct
    # use of CacheManager or a raw cache file read either.
    import_pattern = re.compile(r"^\s*(?:from|import)\s+src\.utils\.cache\b", re.MULTILINE)
    offenders = [
        path.name
        for path in (SRC_ROOT / "dashboard").glob("*.py")
        if import_pattern.search(path.read_text())
    ]
    assert offenders == [], (
        f"src/dashboard must never read the market-data cache directly -- prices "
        f"must flow through MarketDataService (via src.analytics.valuation), not "
        f"src.utils.cache (DECISIONS.md, ADR-0042). Offending files: {offenders}"
    )


def test_dashboard_only_touches_market_data_service_via_analytics_valuation():
    # Positive half: MarketDataService is only ever constructed inside
    # src.analytics.valuation (the one sanctioned touchpoint), never
    # directly inside src.dashboard itself -- confirms the boundary is
    # real, not just "never bypassed by accident because it's never
    # used at all."
    valuation_source = (SRC_ROOT / "analytics" / "valuation.py").read_text()
    assert "MarketDataService" in valuation_source

    direct_construction = re.compile(r"MarketDataService\s*\(")
    offenders = [
        path.name
        for path in (SRC_ROOT / "dashboard").glob("*.py")
        if direct_construction.search(path.read_text())
    ]
    assert offenders == [], (
        f"src/dashboard must not construct MarketDataService itself -- it should "
        f"go through src.analytics.valuation.latest_price()/PortfolioValuationService "
        f"(DECISIONS.md, ADR-0042). Offending files: {offenders}"
    )


def test_account_state_equity_is_computed_from_frozen_entry_price_only():
    from src.risk.models import SizingDecision

    broker = PaperBroker(starting_cash=100_000)
    decision = SizingDecision(
        approved=True, position_size=10.0, capital_allocated=1_000.0, reason="test"
    )
    signal = Signal(
        timestamp=pd.Timestamp("2024-01-01"),
        symbol="SPY",
        direction=SignalDirection.LONG,
        confidence=0.8,
    )
    broker.submit_signal(signal, "SPY", fill_price=100.0, sizing_decision=decision)

    position = broker.positions["SPY"]
    # equity is exactly cash + quantity * entry_price for every open
    # position -- there is no separate "current price" input anywhere in
    # this calculation (src/execution/engine.py, account_state property).
    expected_equity = broker.cash + position.quantity * position.entry_price
    assert broker.account_state.equity == pytest.approx(expected_equity)
