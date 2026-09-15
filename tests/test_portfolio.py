"""Tests for the neutral account/portfolio domain model (src/portfolio).

`AccountState` moved here from `src/risk` (`DECISIONS.md`, ADR-0031) --
its semantics and validation are unchanged, only its location and the
dependency direction around it. These tests mirror the ones that used
to live in `tests/test_risk.py` before the move.
"""

from __future__ import annotations

import pytest

from src.portfolio import AccountState as ExportedAccountState
from src.portfolio.models import AccountState


def test_exported_from_package_init_is_the_same_class():
    assert ExportedAccountState is AccountState


def test_rejects_non_positive_equity():
    with pytest.raises(ValueError):
        AccountState(equity=0)
    with pytest.raises(ValueError):
        AccountState(equity=-100)


def test_rejects_negative_open_exposure():
    with pytest.raises(ValueError):
        AccountState(equity=10_000, open_exposure=-1)


def test_defaults_open_exposure_to_zero():
    account = AccountState(equity=10_000)
    assert account.open_exposure == 0.0


def test_accepts_valid_equity_and_open_exposure():
    account = AccountState(equity=100_000, open_exposure=25_000)
    assert account.equity == 100_000
    assert account.open_exposure == 25_000
