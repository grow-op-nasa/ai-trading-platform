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


# ---------------------------------------------------------------------------
# Sprint 10 (DECISIONS.md, ADR-0043): AI is a signal source, not the
# trading engine. src/ai must not depend on src.broker/src.execution/
# src.risk/src.portfolio/src.dashboard, must never import yfinance
# directly, and the pipeline it feeds into (Backtester, Risk, Execution,
# Dashboard) must stay exactly as AI-agnostic as it was before Sprint 10.
# ---------------------------------------------------------------------------


def test_ai_package_does_not_depend_on_broker_execution_risk_portfolio_or_dashboard():
    # Checks actual import statements, not prose -- src/ai's own
    # docstrings legitimately *mention* these packages to explain the
    # dependency direction, without importing them.
    import_pattern = re.compile(
        r"^\s*(?:from|import)\s+src\.(broker|execution|risk|portfolio|dashboard)\b",
        re.MULTILINE,
    )
    offenders = {}
    for path in (SRC_ROOT / "ai").glob("*.py"):
        match = import_pattern.search(path.read_text())
        if match:
            offenders[path.name] = match.group(1)
    assert offenders == {}, (
        f"src/ai must not depend on src.broker/src.execution/src.risk/"
        f"src.portfolio/src.dashboard -- AI is one more signal source "
        f"feeding the existing Strategy -> Signal -> Risk -> Execution "
        f"pipeline, never a replacement for any of it (DECISIONS.md, "
        f"ADR-0043). Offending files: {offenders}"
    )


def test_ai_package_never_imports_yfinance_directly():
    import_pattern = re.compile(
        r"^\s*(?:from\s+yfinance\b|import\s+yfinance\b|"
        r"from\s+src\.data\.yfinance_provider\s+import|"
        r"import\s+src\.data\.yfinance_provider\b|"
        r"from\s+src\.data(?:\.\w+)?\s+import\s+.*\bYFinanceProvider\b)",
        re.MULTILINE,
    )
    offenders = [
        path.name
        for path in (SRC_ROOT / "ai").glob("*.py")
        if import_pattern.search(path.read_text())
    ]
    assert offenders == [], (
        f"src/ai must consume market data only via a canonical "
        f"CandleDataset (src.data.models), never yfinance/YFinanceProvider "
        f"directly (DECISIONS.md, ADR-0043, Sprint 10 spec section 33). "
        f"Offending files: {offenders}"
    )


def test_ai_signal_strategy_emits_canonical_signal_objects():
    # The positive half: AISignalStrategy actually builds and returns
    # real src.signals.models.Signal objects, the same as every other
    # strategy -- never a second, AI-specific "decision" model.
    #
    # src.strategies.ai_signal transitively imports src.ai.model, which
    # imports scikit-learn -- not installed in every environment this
    # suite runs in (same gap tests/test_dashboard_smoke.py has for
    # Streamlit), so this one test skips cleanly rather than failing
    # the whole architecture module at collection time.
    pytest.importorskip("sklearn")
    from src.strategies.ai_signal import AISignalStrategy

    import inspect

    source = inspect.getsource(AISignalStrategy)
    assert "Signal" in source
    assert "SignalDirection" in source
    # And it must not invent a parallel domain model instead. Use a
    # word-boundary regex rather than a plain substring check: "class
    # AISignal" is itself a substring of "class AISignalStrategy" (the
    # real, intended class), which a naive `in` check would flag as a
    # false positive.
    text = (SRC_ROOT / "strategies" / "ai_signal.py").read_text()
    assert re.search(r"class AISignal\b(?!Strategy)", text) is None


def test_backtester_has_no_ai_specific_branch():
    text = (SRC_ROOT / "backtesting" / "engine.py").read_text()
    for forbidden in ("ai_signal", "AISignalStrategy", "src.ai", "import src.ai"):
        assert forbidden not in text, (
            f"Backtester must remain generic -- it should never special-case "
            f"AI strategies by name or import src.ai (DECISIONS.md, ADR-0043, "
            f"Sprint 10 spec section 66). Found {forbidden!r}."
        )


def test_risk_and_execution_do_not_depend_on_src_ai():
    import_pattern = re.compile(r"^\s*(?:from|import)\s+src\.ai\b", re.MULTILINE)
    for package_name in ("risk", "execution", "portfolio"):
        for path in (SRC_ROOT / package_name).glob("*.py"):
            match = import_pattern.search(path.read_text())
            assert match is None, (
                f"src/{package_name} must stay AI-agnostic -- a model's "
                f"predictions arrive only as an ordinary Signal, never as a "
                f"direct src.ai dependency (DECISIONS.md, ADR-0043). Found in "
                f"{path.name}."
            )


def test_dashboard_and_analytics_do_not_depend_on_src_ai():
    import_pattern = re.compile(r"^\s*(?:from|import)\s+src\.ai\b", re.MULTILINE)
    for package_name in ("dashboard", "analytics"):
        for path in (SRC_ROOT / package_name).glob("*.py"):
            match = import_pattern.search(path.read_text())
            assert match is None, (
                f"src/{package_name} must stay AI-agnostic in Sprint 10 -- no "
                f"AI-specific dashboard page or analytics metric was added "
                f"this sprint (DECISIONS.md, ADR-0043, Sprint 10 spec section "
                f"55/67). Found in {path.name}."
            )


def test_ai_model_interface_has_no_trading_vocabulary():
    # AIModel must never grow an order/broker/portfolio/risk-shaped
    # method -- it predicts a class and a probability, nothing else
    # (Sprint 10 spec, section 13). src.ai.model imports scikit-learn --
    # skip cleanly where it isn't installed (see the test above).
    pytest.importorskip("sklearn")
    from src.ai.model import AIModel

    forbidden_methods = (
        "submit_order",
        "place_order",
        "size_position",
        "allocate_capital",
        "mark_to_market",
    )
    for method_name in forbidden_methods:
        assert not hasattr(AIModel, method_name)


# ---------------------------------------------------------------------------
# Sprint 11 (DECISIONS.md, ADR-0044): Backtester may depend on Risk/
# Portfolio to run the portfolio-aware path, but the dependency is
# strictly one-directional -- Risk/Portfolio never depend back on
# Backtester, Backtester never talks to a real broker or yfinance
# directly, and it never reimplements any risk-sizing arithmetic of its
# own (it composes PortfolioRiskEngine, never recomputes what it returns).
# ---------------------------------------------------------------------------


def test_risk_and_portfolio_do_not_depend_on_backtesting():
    import_pattern = re.compile(r"^\s*(?:from|import)\s+src\.backtesting\b", re.MULTILINE)
    for package_name in ("risk", "portfolio"):
        for path in (SRC_ROOT / package_name).glob("*.py"):
            match = import_pattern.search(path.read_text())
            assert match is None, (
                f"src/{package_name} must not depend on src.backtesting -- the "
                f"dependency direction is strictly Backtester -> Risk/Portfolio, "
                f"never the reverse (DECISIONS.md, ADR-0044). Found in {path.name}."
            )


def test_backtesting_package_never_imports_broker_or_yfinance():
    import_pattern = re.compile(
        r"^\s*(?:from\s+src\.broker\b|import\s+src\.broker\b|"
        r"from\s+yfinance\b|import\s+yfinance\b|"
        r"from\s+src\.data\.yfinance_provider\s+import|"
        r"import\s+src\.data\.yfinance_provider\b|"
        r"from\s+src\.data(?:\.\w+)?\s+import\s+.*\bYFinanceProvider\b)",
        re.MULTILINE,
    )
    offenders = [
        path.name
        for path in (SRC_ROOT / "backtesting").glob("*.py")
        if import_pattern.search(path.read_text())
    ]
    assert offenders == [], (
        f"src/backtesting must never talk to a real broker or fetch data "
        f"directly from yfinance -- the portfolio-aware path simulates fills "
        f"against a fresh, isolated Portfolio only (DECISIONS.md, ADR-0044, "
        f"Sprint 11 spec sections 14, 32). Offending files: {offenders}"
    )


def test_portfolio_backtest_engine_does_not_duplicate_risk_sizing_arithmetic():
    # Mirrors test_execution_does_not_duplicate_risk_sizing_logic()'s own
    # reasoning: PortfolioBacktestEngine must construct and call
    # PortfolioRiskEngine, never reimplement risk_amount/risk_quantity/
    # capital_quantity/allocation_quantity/*_exposure_quantity math of
    # its own. Checked as forbidden *assignment* patterns (an
    # engine.decision.risk_quantity *read* is fine and expected) --
    # a bare substring check would false-positive on those reads.
    text = (SRC_ROOT / "backtesting" / "portfolio_engine.py").read_text()
    forbidden_assignments = re.compile(
        r"\b(risk_amount|risk_quantity|capital_quantity|allocation_quantity|"
        r"portfolio_exposure_quantity|symbol_exposure_quantity)\s*="
    )
    match = forbidden_assignments.search(text)
    assert match is None, (
        f"PortfolioBacktestEngine must never compute its own "
        f"{match.group(1) if match else ''} -- that arithmetic belongs "
        f"exclusively to PortfolioRiskEngine (DECISIONS.md, ADR-0044, Sprint "
        f"11 spec sections 10-11). Found an assignment to a risk-computation "
        f"variable name in portfolio_engine.py."
    )
    # Positive half: it does actually call the real engine.
    assert "PortfolioRiskEngine(" in text
    assert ".decide(" in text
    assert ".decide_close(" in text


def test_portfolio_backtest_engine_has_no_ai_specific_branch():
    # The same architectural guarantee test_backtester_has_no_ai_specific_
    # branch() pins for the legacy path (DECISIONS.md, ADR-0043) --
    # extended to the new portfolio-aware engine (ADR-0044, Sprint 11 spec
    # section 29): EMACrossStrategy, RSIMeanReversionStrategy, and
    # AISignalStrategy must all traverse the identical code path here.
    text = (SRC_ROOT / "backtesting" / "portfolio_engine.py").read_text()
    for forbidden in ("ai_signal", "AISignalStrategy", "src.ai", "import src.ai"):
        assert forbidden not in text, (
            f"PortfolioBacktestEngine must remain generic -- it should never "
            f"special-case AI strategies by name or import src.ai "
            f"(DECISIONS.md, ADR-0044, Sprint 11 spec section 29). Found "
            f"{forbidden!r}."
        )


# ---------------------------------------------------------------------------
# Sprint 12 (DECISIONS.md, ADR-0045): execution-realism boundaries --
# Backtester -> execution abstraction -> Fill, Execution never generates
# Signals or sizes risk, Execution never imports broker-specific adapters,
# generic simulated execution stays broker-independent.
# ---------------------------------------------------------------------------


def test_execution_model_does_not_import_broker_adapters_or_src_broker():
    text = (SRC_ROOT / "backtesting" / "execution_model.py").read_text()
    forbidden = re.compile(
        r"^\s*(?:from|import)\s+(?:src\.broker\b|alpaca|ibkr|ig_markets|tiger)",
        re.IGNORECASE | re.MULTILINE,
    )
    assert forbidden.search(text) is None, (
        "src/backtesting/execution_model.py must remain broker-independent -- "
        "the generic simulated ExecutionModel represents research friction, "
        "never a specific broker's real fee/slippage schedule (DECISIONS.md, "
        "ADR-0045, Sprint 12 spec sections 13, 23, 48)."
    )


def test_execution_model_never_imports_risk_engine_or_generates_signals():
    # Execution decides when/whether/at what price an already-approved
    # order fills -- it must never import PortfolioRiskEngine (sizing is
    # Risk's job) or src.signals (generating a Signal is Strategy's job).
    text = (SRC_ROOT / "backtesting" / "execution_model.py").read_text()
    forbidden_imports = re.compile(
        r"^\s*(?:from|import)\s+src\.(risk\.portfolio_risk|signals)\b", re.MULTILINE
    )
    assert forbidden_imports.search(text) is None, (
        "src/backtesting/execution_model.py must not import "
        "src.risk.portfolio_risk (sizing) or src.signals (signal "
        "generation) -- Execution consumes an already-approved Order, it "
        "never decides whether to trade or how much (DECISIONS.md, "
        "ADR-0045, Sprint 12 spec section 2)."
    )
    assert "class Signal" not in text
    assert "PortfolioRiskEngine(" not in text


def test_execution_model_has_no_hidden_random_state_or_system_time():
    # Purity requirement (Sprint 12 spec section 52): same inputs, same
    # output -- no dependence on global random state or wall-clock time.
    text = (SRC_ROOT / "backtesting" / "execution_model.py").read_text()
    forbidden = re.compile(r"\b(random\.|np\.random|datetime\.now\(|pd\.Timestamp\.now\()")
    assert forbidden.search(text) is None, (
        "src/backtesting/execution_model.py must be pure -- no random "
        "module usage and no wall-clock reads (DECISIONS.md, ADR-0045, "
        "Sprint 12 spec sections 12, 52)."
    )


def test_portfolio_backtest_engine_routes_fills_through_the_execution_model():
    # Positive half of the Sprint 12 boundary: PortfolioBacktestEngine
    # must actually call the real ExecutionModel rather than computing
    # its own fill price/timing/cost inline.
    text = (SRC_ROOT / "backtesting" / "portfolio_engine.py").read_text()
    assert "ExecutionModel(" in text
    assert ".simulate(" in text


def test_backtesting_execution_model_reuses_the_existing_fill_and_order_shapes():
    # Sprint 12 spec section 4: use the existing Fill/Order models --
    # never a second, incompatible fill domain model.
    text = (SRC_ROOT / "backtesting" / "execution_model.py").read_text()
    assert "from src.execution.models import" in text
    assert "class Fill" not in text
    assert "class Order" not in text


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


# ---------------------------------------------------------------------------
# Sprint 13 (DECISIONS.md, ADR-0046): AI Research Agent boundaries.
# ---------------------------------------------------------------------------

_AGENTS_DIR = SRC_ROOT / "ai" / "agents"


def _agent_source_files() -> list[pathlib.Path]:
    return sorted(p for p in _AGENTS_DIR.glob("*.py") if p.name != "anthropic_provider.py")


def test_agent_core_does_not_import_broker_adapters():
    forbidden = re.compile(r"^\s*(?:from|import)\s+src\.broker\b", re.MULTILINE)
    offenders = [p.name for p in _agent_source_files() if forbidden.search(p.read_text())]
    assert offenders == [], (
        f"src/ai/agents (agent core) must never import a broker adapter -- the "
        f"research agent has no live/paper execution capability at all "
        f"(DECISIONS.md, ADR-0046, Sprint 13 spec sections 31, 92). Offending "
        f"files: {offenders}"
    )


def test_agent_core_does_not_import_execution_engine():
    # PaperBroker itself (src.execution.engine) is trading infrastructure
    # the agent must never touch -- src.execution.models (plain Order/
    # Fill/OrderSide shapes, reused by ExecutionModel) is fine and is not
    # checked for here.
    forbidden = re.compile(r"^\s*(?:from|import)\s+src\.execution\.engine\b", re.MULTILINE)
    offenders = [p.name for p in _agent_source_files() if forbidden.search(p.read_text())]
    assert offenders == [], (
        f"src/ai/agents must never import src.execution.engine (PaperBroker) "
        f"-- offending files: {offenders}"
    )


def test_agent_core_does_not_import_yfinance_or_any_data_provider_directly():
    forbidden = re.compile(
        r"^\s*(?:from|import)\s+(?:yfinance|src\.data\.yfinance_provider)\b", re.MULTILINE
    )
    offenders = [p.name for p in _agent_source_files() if forbidden.search(p.read_text())]
    assert offenders == [], (
        f"src/ai/agents must reach market data only through "
        f"src.data.MarketDataService (via src.research.trial_service), never a "
        f"provider implementation directly (Sprint 13 spec, section 54). "
        f"Offending files: {offenders}"
    )


def test_agent_core_has_no_shell_or_arbitrary_filesystem_access():
    # Matches actual invocation syntax (a dotted call or a bare eval/exec
    # call) rather than the bare word -- several of these modules'
    # docstrings legitimately *mention* "shell command" or "subprocess"
    # in prose explaining what must never happen, without ever calling
    # one.
    forbidden = re.compile(r"subprocess\.\w+\(|os\.system\(|os\.popen\(|\beval\(|\bexec\(")
    offenders = [p.name for p in _agent_source_files() if forbidden.search(p.read_text())]
    assert offenders == [], (
        f"src/ai/agents must never shell out or eval/exec arbitrary code -- "
        f"the LLM must never be allowed to call arbitrary Python (Sprint 13 "
        f"spec, sections 15, 33, 103). Offending files: {offenders}"
    )
    import_forbidden = re.compile(r"^\s*(?:from|import)\s+subprocess\b", re.MULTILINE)
    import_offenders = [
        p.name for p in _agent_source_files() if import_forbidden.search(p.read_text())
    ]
    assert import_offenders == [], (
        f"src/ai/agents must never import subprocess at all -- offending "
        f"files: {import_offenders}"
    )


def test_agent_core_does_not_import_anthropic_at_module_level():
    # The provider abstraction (provider.py) and the agent loop/tools
    # must stay provider-agnostic -- only anthropic_provider.py may
    # mention anthropic, and even there only via a lazy, in-function
    # import (mirrors src.research.renderers.ClaudeNarrativeRenderer).
    forbidden = re.compile(r"^\s*(?:from|import)\s+anthropic\b", re.MULTILINE)
    offenders = [p.name for p in _agent_source_files() if forbidden.search(p.read_text())]
    assert offenders == [], (
        f"only src/ai/agents/anthropic_provider.py may reference the "
        f"anthropic package, and only via a lazy import inside a function "
        f"(Sprint 13 spec, section 5). Offending files: {offenders}"
    )
    anthropic_provider_text = (_AGENTS_DIR / "anthropic_provider.py").read_text()
    assert re.search(r"^import anthropic\b", anthropic_provider_text, re.MULTILINE) is None, (
        "anthropic_provider.py must import anthropic lazily, inside generate(), "
        "not at module level -- the base platform must not require it "
        "(Sprint 13 spec, section 7)."
    )


def test_anthropic_is_not_a_hard_dependency_in_requirements_txt():
    requirements_text = (
        SRC_ROOT.parent / "requirements.txt"
    ).read_text()
    assert "anthropic" not in requirements_text.lower(), (
        "the base platform must work without the anthropic package installed "
        "(Sprint 13 spec, section 7) -- do not add it to requirements.txt."
    )


def test_research_reporter_does_not_depend_on_the_agent_runtime():
    # The Agent may consume ResearchReporter's output; ResearchReporter
    # must never depend on the Agent (Sprint 13 spec, section 93/68).
    forbidden = re.compile(r"^\s*(?:from|import)\s+src\.ai\.agents\b", re.MULTILINE)
    for path in (SRC_ROOT / "research").glob("*.py"):
        text = path.read_text()
        assert forbidden.search(text) is None, (
            f"{path.name} (src/research) must not depend on src.ai.agents -- "
            f"the dependency direction is Agent -> research primitives, never "
            f"the reverse (DECISIONS.md, ADR-0046)."
        )


def test_research_trial_service_uses_the_portfolio_backtest_engine():
    # Sprint 13 spec, section 76: the historical backtest tool must use
    # MarketDataService -> Strategy -> PortfolioBacktestEngine -> Risk ->
    # Execution -> Analytics -- never a second backtest pipeline.
    text = (SRC_ROOT / "research" / "trial_service.py").read_text()
    assert "from src.data.service import MarketDataService" in text
    assert "from src.backtesting.engine import Backtester" in text
    assert ".run_portfolio(" in text
    assert "from src.analytics.service import AnalyticsService" in text
    assert "from src.backtesting.execution_model import" in text


def test_default_tool_registry_exposes_exactly_the_seven_sprint13_tools():
    pytest.importorskip("joblib")  # transitively required by src.ai.registry.ModelRegistry
    from src.ai.agents.tools import default_tool_registry

    registry = default_tool_registry(
        experiment_registry=ExperimentRegistry(db_path=":memory:"),
    )
    names = {t.name for t in registry.all_tools()}
    assert names == {
        "list_experiments",
        "get_experiment",
        "analyze_experiment",
        "compare_experiments",
        "list_strategies",
        "get_model_metadata",
        "run_historical_backtest",
    }


def test_agent_policy_default_denies_every_trading_and_mutation_capability():
    from src.ai.agents.policy import ResearchAgentPolicy

    policy = ResearchAgentPolicy()
    assert policy.allow_live_trading is False
    assert policy.allow_portfolio_mutation is False
    assert policy.allow_strategy_generation is False
    assert policy.allow_model_training is False


# ---------------------------------------------------------------------------
# Sprint 14 (DECISIONS.md, ADR-0047): AI Strategy Development Agent
# boundaries. The core claim this whole section protects: AI can CREATE,
# TEST, and ITERATE on a candidate; it cannot PROMOTE it, TRADE it, or
# MODIFY the production strategy registry -- and none of that depends on
# prompt language, only on what code exists and what imports what.
# ---------------------------------------------------------------------------

_STRATEGY_DEV_DIR = SRC_ROOT / "ai" / "agents" / "strategy_dev"


def _strategy_dev_source_files() -> list[pathlib.Path]:
    return sorted(_STRATEGY_DEV_DIR.glob("*.py"))


def test_strategy_dev_package_does_not_import_broker_or_execution_engine():
    forbidden = re.compile(r"^\s*(?:from|import)\s+src\.(broker|execution\.engine)\b", re.MULTILINE)
    offenders = {
        p.name: forbidden.search(p.read_text()).group(1)
        for p in _strategy_dev_source_files()
        if forbidden.search(p.read_text())
    }
    assert offenders == {}, (
        f"src/ai/agents/strategy_dev must never import a broker adapter or "
        f"src.execution.engine (PaperBroker) -- the Strategy Development "
        f"Agent has no live/paper execution capability at all (DECISIONS.md, "
        f"ADR-0047). Offending files: {offenders}"
    )


def test_strategy_dev_package_does_not_import_portfolio_or_risk_mutation():
    # The agent's own code may legitimately reuse the *read* path through
    # ResearchTrialService -> PortfolioBacktestEngine (which itself
    # composes Risk/Portfolio/Execution) -- but strategy_dev's own
    # modules must never import src.portfolio/src.risk directly to
    # mutate state themselves.
    forbidden = re.compile(r"^\s*(?:from|import)\s+src\.(portfolio|risk)\b", re.MULTILINE)
    offenders = {
        p.name: forbidden.search(p.read_text()).group(1)
        for p in _strategy_dev_source_files()
        if forbidden.search(p.read_text())
    }
    assert offenders == {}, (
        f"src/ai/agents/strategy_dev must not import src.portfolio/src.risk "
        f"directly -- backtest evidence flows only through "
        f"ResearchTrialService's existing pipeline (DECISIONS.md, ADR-0047). "
        f"Offending files: {offenders}"
    )


def test_strategy_dev_package_does_not_import_dashboard_or_cli():
    # The promotion CLI (src.cli.strategy_promote) is intentionally the
    # other direction: it may import strategy_dev, strategy_dev must
    # never import it or anything else under src.cli/src.dashboard.
    forbidden = re.compile(r"^\s*(?:from|import)\s+src\.(dashboard|cli)\b", re.MULTILINE)
    offenders = {
        p.name: forbidden.search(p.read_text()).group(1)
        for p in _strategy_dev_source_files()
        if forbidden.search(p.read_text())
    }
    assert offenders == {}, (
        f"src/ai/agents/strategy_dev must never import src.dashboard or "
        f"src.cli -- offending files: {offenders}"
    )


def test_strategy_promote_is_never_imported_by_the_agent_package():
    # The positive-direction check complementing the one above: confirm
    # the human-only promotion command specifically is never reachable
    # from any strategy_dev module (Sprint 14 spec, sections 57, 92-93).
    forbidden = re.compile(r"^\s*(?:from|import)\s+src\.cli\.strategy_promote\b", re.MULTILINE)
    offenders = [p.name for p in _strategy_dev_source_files() if forbidden.search(p.read_text())]
    assert offenders == [], (
        f"src.cli.strategy_promote must never be imported by "
        f"src/ai/agents/strategy_dev -- promotion is human-only and has no "
        f"agent-reachable code path (DECISIONS.md, ADR-0047). Offending "
        f"files: {offenders}"
    )


def test_no_tool_wraps_candidate_registry_promote():
    # tools.py may import CandidateRegistry (it does, to create/inspect/
    # validate/test/freeze/report candidates) but must never call
    # .promote() itself -- that would give the agent a reachable path to
    # production promotion, defeating the entire safety boundary.
    text = (_STRATEGY_DEV_DIR / "tools.py").read_text()
    assert ".promote(" not in text, (
        "src/ai/agents/strategy_dev/tools.py must never call "
        "CandidateRegistry.promote() -- promotion is reachable only via "
        "src.cli.strategy_promote (DECISIONS.md, ADR-0047)."
    )


def test_strategy_registry_has_no_dependency_on_the_agent_package():
    # The reverse-direction check: production StrategyRegistry must stay
    # completely unaware that an AI agent or a candidate layer exists.
    forbidden = re.compile(r"^\s*(?:from|import)\s+src\.ai\.agents\b", re.MULTILINE)
    text = (SRC_ROOT / "strategies" / "registry.py").read_text()
    assert forbidden.search(text) is None, (
        "src/strategies/registry.py (the production StrategyRegistry) must "
        "not depend on src.ai.agents in any direction -- candidates are "
        "promoted into it by a human process, it never reaches back into "
        "agent code (DECISIONS.md, ADR-0047)."
    )


def test_backtester_has_no_candidate_specific_branch():
    # Mirrors test_backtester_has_no_ai_specific_branch()'s own reasoning
    # (ADR-0043) for the Sprint 14 candidate layer: PrecomputedSignalStrategy
    # is an ordinary Strategy from the engine's point of view -- the
    # Backtester/PortfolioBacktestEngine must never special-case it or
    # CandidateStrategy by name (Sprint 14 spec, section 111).
    forbidden_mentions = ("CandidateStrategy", "PrecomputedSignalStrategy", "strategy_dev", "src.ai.agents")
    for filename in ("engine.py", "portfolio_engine.py"):
        text = (SRC_ROOT / "backtesting" / filename).read_text()
        for mention in forbidden_mentions:
            assert mention not in text, (
                f"src/backtesting/{filename} must remain fully generic -- it "
                f"must never mention {mention!r} (DECISIONS.md, ADR-0047, "
                f"Sprint 14 spec section 111). The candidate runner's "
                f"PrecomputedSignalStrategy is presented to the Backtester as "
                f"an ordinary Strategy, with zero special-casing."
            )


def test_research_trial_service_used_by_candidates_is_the_same_unmodified_service():
    # Positive half: run_candidate_backtest must reuse
    # ResearchTrialService.run_trial_with_strategy -- never a second,
    # candidate-specific backtest pipeline (Sprint 14 spec, sections 31,
    # 115).
    text = (_STRATEGY_DEV_DIR / "tools.py").read_text()
    assert "from src.research.trial_service import ResearchTrialService" in text
    assert ".run_trial_with_strategy(" in text


def test_safety_allowlist_never_includes_a_forbidden_platform_package():
    # Static check on the actual allowlist data, not just its docstring
    # claim: no entry under src.broker/src.execution/src.portfolio/
    # src.risk/src.dashboard/src.cli/src.ai/src.research ever appears in
    # ALLOWED_PLATFORM_MODULES, whatever gets added to it in the future
    # (Sprint 14 spec, sections 10, 14-18).
    from src.ai.agents.strategy_dev.safety import ALLOWED_PLATFORM_MODULES

    forbidden_prefixes = (
        "src.broker",
        "src.execution",
        "src.portfolio",
        "src.risk",
        "src.dashboard",
        "src.cli",
        "src.ai",
        "src.research",
        "src.data",
    )
    offenders = [
        module
        for module in ALLOWED_PLATFORM_MODULES
        if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes)
    ]
    assert offenders == [], (
        f"ALLOWED_PLATFORM_MODULES must never include a module under "
        f"broker/execution/portfolio/risk/dashboard/cli/ai/research/data -- "
        f"a candidate strategy may only import the Strategy/Signal SDK and "
        f"indicator engine (DECISIONS.md, ADR-0047). Offending entries: "
        f"{offenders}"
    )


def test_safety_forbidden_names_include_register_strategy():
    from src.ai.agents.strategy_dev.safety import FORBIDDEN_NAMES

    assert "register_strategy" in FORBIDDEN_NAMES, (
        "a candidate must never be able to self-register into the "
        "production StrategyRegistry (Sprint 14 spec, sections 39, 91)."
    )


def test_subprocess_usage_is_confined_to_the_candidate_runner():
    # Sprint 14 spec, sections 8, 29: the platform *may* internally use a
    # controlled subprocess for candidate execution, but that must live
    # in exactly one platform-owned module (runner.py) -- never in
    # tools.py, dev_agent.py, or anywhere else an agent tool call could
    # reach it directly.
    subprocess_pattern = re.compile(r"^\s*(?:from|import)\s+subprocess\b|subprocess\.\w+\(", re.MULTILINE)
    offenders = [
        p.name
        for p in _strategy_dev_source_files()
        if p.name != "runner.py" and subprocess_pattern.search(p.read_text())
    ]
    assert offenders == [], (
        f"subprocess must be used only by runner.py within "
        f"src/ai/agents/strategy_dev -- offending files: {offenders}"
    )


def test_strategy_dev_package_never_eval_or_execs_anything_itself():
    # The platform's own code must never eval/exec -- safety.py only
    # *detects and rejects* eval/exec/compile in candidate source, it
    # must never call them (Sprint 14 spec, section 11).
    forbidden = re.compile(r"\beval\(|\bexec\(|\bcompile\(|__import__\(")
    offenders = []
    for p in _strategy_dev_source_files():
        text = p.read_text()
        # safety.py legitimately *names* these as forbidden strings inside
        # its own denylist/docstrings -- only flag an actual call syntax
        # appearing outside of a string/comment context is hard to do
        # perfectly with regex, so this checks for the call pattern only,
        # which safety.py's own denylist entries (bare identifiers in a
        # frozenset) do not trigger.
        if forbidden.search(text):
            offenders.append(p.name)
    assert offenders == [], (
        f"src/ai/agents/strategy_dev must never itself call eval/exec/"
        f"compile/__import__ -- it only detects and rejects them in "
        f"candidate source (DECISIONS.md, ADR-0047). Offending files: "
        f"{offenders}"
    )


def test_importlib_dynamic_loading_is_confined_to_the_harness_and_lazy_init():
    # importlib is used exactly twice, both sanctioned: _harness.py loads
    # the (already statically-validated) candidate module, and
    # __init__.py's PEP 562 __getattr__ lazily imports this package's own
    # submodules. No other module may dynamically import anything.
    forbidden = re.compile(r"^\s*(?:from|import)\s+importlib\b", re.MULTILINE)
    offenders = [
        p.name
        for p in _strategy_dev_source_files()
        if p.name not in ("_harness.py", "__init__.py") and forbidden.search(p.read_text())
    ]
    assert offenders == [], (
        f"importlib must be confined to _harness.py (candidate loading) and "
        f"__init__.py (lazy package attributes) -- offending files: "
        f"{offenders}"
    )


def test_default_dev_tool_registry_exposes_exactly_the_fourteen_sprint14_tools():
    pytest.importorskip("joblib")  # transitively required by src.ai.registry.ModelRegistry
    from src.ai.agents.strategy_dev.tools import default_dev_tool_registry

    registry = default_dev_tool_registry(
        experiment_registry=ExperimentRegistry(db_path=":memory:"),
    )
    names = {t.name for t in registry.all_tools()}
    assert names == {
        "list_strategies",
        "list_experiments",
        "get_experiment",
        "analyze_experiment",
        "compare_experiments",
        "list_indicators",
        "create_candidate_strategy",
        "inspect_candidate",
        "validate_candidate",
        "test_candidate",
        "run_candidate_backtest",
        "compare_candidate_to_baseline",
        "freeze_candidate",
        "get_candidate_report",
    }


def test_default_dev_tool_registry_has_no_filesystem_or_shell_tool():
    pytest.importorskip("joblib")
    from src.ai.agents.strategy_dev.tools import default_dev_tool_registry

    registry = default_dev_tool_registry(experiment_registry=ExperimentRegistry(db_path=":memory:"))
    names = {t.name for t in registry.all_tools()}
    forbidden_substrings = ("write_file", "read_file", "delete_file", "list_directory", "shell", "bash", "exec")
    for name in names:
        for substring in forbidden_substrings:
            assert substring not in name, (
                f"tool {name!r} looks like a generic filesystem/shell "
                f"capability -- Sprint 14 spec sections 7-8 forbid both."
            )


def test_strategy_dev_agent_policy_default_denies_every_dangerous_capability():
    from src.ai.agents.strategy_dev.policy import StrategyDevelopmentAgentPolicy

    policy = StrategyDevelopmentAgentPolicy()
    assert policy.allow_live_trading is False
    assert policy.allow_paper_trading is False
    assert policy.allow_portfolio_mutation is False
    assert policy.allow_risk_mutation is False
    assert policy.allow_execution_mutation is False
    assert policy.allow_model_training is False
    assert policy.allow_production_strategy_mutation is False
    assert policy.allow_production_strategy_registration is False
    assert policy.allow_git_mutation is False
    assert policy.allow_auto_promotion is False


def test_candidate_workspace_default_dir_falls_under_the_wholesale_data_gitignore():
    from src.ai.agents.strategy_dev.workspace import DEFAULT_CANDIDATES_DIR
    from src.ai.agents.strategy_dev.store import DEFAULT_DEV_RUNS_DIR

    assert str(DEFAULT_CANDIDATES_DIR).startswith("data/")
    assert str(DEFAULT_DEV_RUNS_DIR).startswith("data/")
    gitignore_text = (SRC_ROOT.parent / ".gitignore").read_text()
    assert re.search(r"^data/\*\s*$", gitignore_text, re.MULTILINE), (
        "the repo's wholesale data/* .gitignore rule must still be present -- "
        "it is what covers data/strategy_candidates/ and "
        "data/strategy_dev_runs/ without a new .gitignore entry "
        "(DECISIONS.md, ADR-0047)."
    )


def test_strategy_dev_never_imports_git_tooling():
    forbidden = re.compile(r"^\s*(?:from|import)\s+git\b|subprocess.*\bgit\b", re.MULTILINE)
    offenders = [p.name for p in _strategy_dev_source_files() if forbidden.search(p.read_text())]
    assert offenders == [], (
        f"src/ai/agents/strategy_dev must never touch Git -- the agent "
        f"cannot commit/push/merge/checkout (DECISIONS.md, ADR-0047). "
        f"Offending files: {offenders}"
    )
