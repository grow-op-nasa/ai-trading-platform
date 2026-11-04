"""Strategy catalogue and family taxonomy (src/strategies/catalogue.py, Sprint 23, ADR-0061)."""

from __future__ import annotations

import importlib

import pytest

from src.strategies.base import Strategy
from src.strategies.catalogue import (
    UNCLASSIFIED_METADATA,
    Directionality,
    ImplementationMaturity,
    StrategyFamily,
    StrategyMetadata,
    build_catalogue,
    family_coverage,
    metadata_for,
)
from src.strategies.registry import _unregister_strategy, available_strategies, get_strategy_class, register_strategy
from src.strategies.sdk import BaseStrategy

importlib.import_module("src.strategies")   # registers the built-in strategies


def _all_registered_including_optional() -> list[str]:
    """ai_signal needs optional ML dependencies (joblib/scikit-learn); include it only where it can be imported."""
    try:
        importlib.import_module("src.strategies.ai_signal")
    except ImportError:
        pass
    return available_strategies()


def test_every_registered_strategy_is_classified():
    # Strategies defined inside test modules (scripted fixtures that other test files register globally) are not product strategies.
    shipped = [n for n in _all_registered_including_optional() if not get_strategy_class(n).__module__.startswith("tests")]
    unclassified = [n for n in shipped if metadata_for(n).family is StrategyFamily.UNCLASSIFIED]
    assert unclassified == [], f"describe these strategies (class METADATA or the catalogue table): {unclassified}"


def test_expected_families_for_the_builtin_strategies():
    assert metadata_for("ema_cross").family is StrategyFamily.TREND_FOLLOWING
    assert metadata_for("ema_cross_vol_filter").family is StrategyFamily.TREND_FOLLOWING
    assert metadata_for("rsi_mean_reversion").family is StrategyFamily.MEAN_REVERSION
    assert metadata_for("donchian_breakout").family is StrategyFamily.BREAKOUT


def test_a_variant_is_labelled_so_it_is_not_counted_as_independent_diversity():
    assert metadata_for("ema_cross_vol_filter").variant_of == "ema_cross"
    assert metadata_for("ema_cross").variant_of is None
    cov = family_coverage(build_catalogue())
    assert "ema_cross_vol_filter (variant of ema_cross)" in cov[StrategyFamily.TREND_FOLLOWING]
    independent = [x for x in cov[StrategyFamily.TREND_FOLLOWING] if "variant" not in x]
    assert independent == ["ema_cross"]


def test_three_distinct_hypothesis_families_are_represented():
    cov = family_coverage(build_catalogue())
    for family in (StrategyFamily.TREND_FOLLOWING, StrategyFamily.MEAN_REVERSION, StrategyFamily.BREAKOUT):
        assert cov[family], f"{family.value} has no strategy"
    assert cov[StrategyFamily.RELATIVE_STRENGTH] == []       # reserved, not pretended


def test_entries_carry_the_audit_attributes():
    by_id = {e.strategy_id: e for e in build_catalogue()}
    e = by_id["donchian_breakout"]
    assert e.name and e.metadata.hypothesis and e.metadata.entry and e.metadata.exit
    assert e.metadata.directionality is Directionality.LONG_FLAT
    assert [p.name for p in e.parameters] == ["entry_lookback", "exit_lookback", "confidence"]
    assert e.history_requirement["min_bars"] == 21
    assert e.metadata.supported_intervals == ("1d",)
    assert e.metadata.known_weaknesses
    assert by_id["ema_cross"].history_requirement["min_bars"] == 61
    assert by_id["rsi_mean_reversion"].history_requirement["min_bars"] == 65
    assert e.source_hash and len(e.source_hash) == 64


def test_maturity_is_derived_not_self_declared():
    for e in build_catalogue():
        assert e.maturity in (ImplementationMaturity.REGISTERED, ImplementationMaturity.PROMOTED)
    assert not hasattr(StrategyMetadata, "maturity")


def test_a_new_strategy_appears_by_declaring_itself_without_editing_the_catalogue():
    meta = StrategyMetadata(
        family=StrategyFamily.MOMENTUM, hypothesis="h", entry="e", exit="x", directionality=Directionality.LONG_FLAT,
        supported_intervals=("1d",), regime_assumptions="r", known_weaknesses=("w",))

    @register_strategy("zz_catalogue_probe")
    class Probe(BaseStrategy):
        METADATA = meta

        def __init__(self, symbol: str) -> None:
            super().__init__(name="zz_catalogue_probe", symbol=symbol)

        def prepare(self, data):
            return data

        def generate_signals(self, data):
            return []

    try:
        assert metadata_for("zz_catalogue_probe") is meta
        entry = {e.strategy_id: e for e in build_catalogue()}["zz_catalogue_probe"]
        assert entry.family is StrategyFamily.MOMENTUM
        assert entry.history_requirement is None and entry.history_note        # no declaration is reported, not invented
        assert isinstance(Probe(symbol="SPY"), Strategy)
    finally:
        _unregister_strategy("zz_catalogue_probe")


def test_an_undescribed_strategy_is_reported_unclassified_never_guessed():
    @register_strategy("zz_undescribed_probe")
    class Bare(BaseStrategy):
        def __init__(self, symbol: str) -> None:
            super().__init__(name="zz_undescribed_probe", symbol=symbol)

        def prepare(self, data):
            return data

        def generate_signals(self, data):
            return []

    try:
        assert metadata_for("zz_undescribed_probe") is UNCLASSIFIED_METADATA
        assert metadata_for("zz_undescribed_probe").family is StrategyFamily.UNCLASSIFIED
    finally:
        _unregister_strategy("zz_undescribed_probe")


def test_unknown_strategy_name_raises():
    with pytest.raises(KeyError):
        metadata_for("does_not_exist")


def test_building_the_catalogue_runs_no_strategy_and_fetches_no_data(monkeypatch):
    import src.data.yfinance_provider as yp

    def boom(*a, **k):
        raise AssertionError("the catalogue must not touch market data")

    monkeypatch.setattr(yp.YFinanceProvider, "fetch_candles", boom, raising=False)
    assert build_catalogue()
