"""Tests for the strategy identity/registry seam (src/strategies/identity.py,
src/strategies/registry.py -- `DECISIONS.md`, ADR-0035).
"""

from __future__ import annotations

import pytest

from src.strategies.identity import strategy_version
from src.strategies.registry import (
    available_strategies,
    get_strategy_class,
    register_strategy,
)
from src.strategies.ema_cross import EMACrossStrategy
from src.strategies.sdk import BaseStrategy


def test_ema_cross_is_registered_under_its_own_name():
    assert get_strategy_class("ema_cross") is EMACrossStrategy


def test_available_strategies_includes_ema_cross():
    assert "ema_cross" in available_strategies()


def test_get_strategy_class_raises_for_unknown_name():
    with pytest.raises(KeyError):
        get_strategy_class("does_not_exist")


def test_register_strategy_rejects_duplicate_names():
    @register_strategy("a_one_off_test_strategy")
    class _First:
        pass

    with pytest.raises(ValueError):

        @register_strategy("a_one_off_test_strategy")
        class _Second:
            pass


def test_strategy_version_is_deterministic_for_the_same_class():
    assert strategy_version(EMACrossStrategy) == strategy_version(EMACrossStrategy)


def test_strategy_version_differs_between_different_classes():
    class OtherStrategy(BaseStrategy):
        def prepare(self, data):
            return data

        def generate_signals(self, data):
            return []

    assert strategy_version(EMACrossStrategy) != strategy_version(OtherStrategy)


def test_strategy_version_changes_when_source_changes():
    # Two classes with different bodies (hence different source) must
    # hash differently -- the entire point of a source-based version:
    # an implementation change is automatically a different version.
    class VersionOne(BaseStrategy):
        def prepare(self, data):
            return data

        def generate_signals(self, data):
            return []

    class VersionTwo(BaseStrategy):
        def prepare(self, data):
            out = data.copy()
            return out

        def generate_signals(self, data):
            return []

    assert strategy_version(VersionOne) != strategy_version(VersionTwo)


def test_ema_cross_strategy_exposes_its_params():
    strategy = EMACrossStrategy(symbol="SPY", fast=10, slow=20, confidence=0.9)
    assert strategy.params == {"fast": 10, "slow": 20, "confidence": 0.9}


def test_base_strategy_default_params_is_empty_dict():
    class Bare(BaseStrategy):
        def prepare(self, data):
            return data

        def generate_signals(self, data):
            return []

    strategy = Bare(name="bare", symbol="SPY")
    assert strategy.params == {}
