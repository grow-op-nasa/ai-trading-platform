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
