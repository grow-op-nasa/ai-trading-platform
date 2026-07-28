"""Tests for `atp doctor` (src/cli/registry.py, checks.py, doctor.py,
__main__.py).

Individual checks are tested with fakes substituted for the real
MarketDataService / CacheManager / ExperimentRegistry so this suite
never touches the network or the real data/cache/experiments.db files
-- consistent with the rest of the project's test doubles pattern
(FakeProvider in test_market_data.py, tmp_path-backed registries in
test_experiments.py).

`run_doctor`/`_format_report`'s aggregation logic is tested against an
isolated, temporarily-emptied check registry so it doesn't depend on
what real checks happen to be registered.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.cli import checks
from src.cli import doctor as doctor_module
from src.cli import registry as cli_registry
from src.cli import __main__ as main_module
from src.cli.__main__ import main
from src.cli.registry import CheckResult, CheckStatus, DuplicateCheckError, register_check


# ---------------------------------------------------------------------------
# registry.py
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_registry():
    """Swap in an empty check registry for the duration of a test, then
    restore whatever was registered before (the real checks)."""
    saved = dict(cli_registry._CHECKS)
    cli_registry._reset_registry_for_tests()
    yield cli_registry
    cli_registry._CHECKS.clear()
    cli_registry._CHECKS.update(saved)


def test_register_check_adds_to_registry(isolated_registry):
    @register_check("Fake Check")
    def fake() -> CheckResult:
        return CheckResult("Fake Check", CheckStatus.OK)

    assert "Fake Check" in cli_registry.registered_checks()


def test_register_check_rejects_duplicate_name(isolated_registry):
    @register_check("Fake Check")
    def fake_one() -> CheckResult:
        return CheckResult("Fake Check", CheckStatus.OK)

    with pytest.raises(DuplicateCheckError):

        @register_check("Fake Check")
        def fake_two() -> CheckResult:
            return CheckResult("Fake Check", CheckStatus.OK)


def test_registered_checks_returns_a_copy(isolated_registry):
    @register_check("Fake Check")
    def fake() -> CheckResult:
        return CheckResult("Fake Check", CheckStatus.OK)

    snapshot = cli_registry.registered_checks()
    snapshot["Injected"] = lambda: CheckResult("Injected", CheckStatus.OK)

    assert "Injected" not in cli_registry.registered_checks()


# ---------------------------------------------------------------------------
# checks.py -- Python Version
# ---------------------------------------------------------------------------

def test_python_version_passes_against_running_interpreter():
    result = checks.check_python_version()
    assert result.status is CheckStatus.OK


def test_python_version_fails_below_minimum(monkeypatch):
    monkeypatch.setattr(checks, "MIN_PYTHON_VERSION", (99, 0))
    result = checks.check_python_version()
    assert result.status is CheckStatus.FAIL
    assert "99" in result.detail


# ---------------------------------------------------------------------------
# checks.py -- Configuration
# ---------------------------------------------------------------------------

def test_configuration_passes_with_real_settings():
    result = checks.check_configuration()
    assert result.status is CheckStatus.OK


def test_configuration_fails_on_missing_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(checks.settings, "DATA_DIR", tmp_path / "does_not_exist")
    result = checks.check_configuration()
    assert result.status is CheckStatus.FAIL


def test_configuration_fails_on_empty_watchlist(monkeypatch):
    monkeypatch.setattr(checks.settings, "WATCHLIST", [])
    result = checks.check_configuration()
    assert result.status is CheckStatus.FAIL
    assert "WATCHLIST" in result.detail


# ---------------------------------------------------------------------------
# checks.py -- Market Data (network faked out)
# ---------------------------------------------------------------------------

class _FakeServiceReturningData:
    def __init__(self, *args, **kwargs):
        pass

    def get_history(self, symbol, period):
        index = pd.date_range("2024-01-01", periods=3, freq="D", name="timestamp")
        return pd.DataFrame(
            {"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
             "close": [1, 2, 3], "volume": [10, 10, 10]},
            index=index,
        )


class _FakeServiceReturningEmpty:
    def __init__(self, *args, **kwargs):
        pass

    def get_history(self, symbol, period):
        return pd.DataFrame()


class _FakeServiceRaising:
    def __init__(self, *args, **kwargs):
        pass

    def get_history(self, symbol, period):
        raise checks.DataProviderError("vendor unreachable")


def test_market_data_passes_when_provider_returns_candles(monkeypatch):
    monkeypatch.setattr(checks, "MarketDataService", _FakeServiceReturningData)
    result = checks.check_market_data()
    assert result.status is CheckStatus.OK
    assert "3 candles" in result.detail


def test_market_data_fails_when_provider_returns_no_rows(monkeypatch):
    monkeypatch.setattr(checks, "MarketDataService", _FakeServiceReturningEmpty)
    result = checks.check_market_data()
    assert result.status is CheckStatus.FAIL


def test_market_data_fails_when_provider_raises(monkeypatch):
    monkeypatch.setattr(checks, "MarketDataService", _FakeServiceRaising)
    result = checks.check_market_data()
    assert result.status is CheckStatus.FAIL
    assert "vendor unreachable" in result.detail


# ---------------------------------------------------------------------------
# checks.py -- Cache
# ---------------------------------------------------------------------------

def test_cache_check_round_trips_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(checks, "DEFAULT_CACHE_DIR", tmp_path)
    result = checks.check_cache()
    assert result.status is CheckStatus.OK
    # the probe file must not be left behind
    assert list(tmp_path.glob("*.csv")) == []


def test_cache_check_fails_when_round_trip_is_broken(monkeypatch, tmp_path):
    monkeypatch.setattr(checks, "DEFAULT_CACHE_DIR", tmp_path)

    class _NoOpCacheManager(checks.CacheManager):
        def set(self, key, data):
            pass  # never actually writes anything

    monkeypatch.setattr(checks, "CacheManager", _NoOpCacheManager)
    result = checks.check_cache()
    assert result.status is CheckStatus.FAIL


# ---------------------------------------------------------------------------
# checks.py -- Experiments DB
# ---------------------------------------------------------------------------

def test_experiments_db_passes_against_tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(checks, "DEFAULT_DB_PATH", tmp_path / "experiments.db")
    result = checks.check_experiments_db()
    assert result.status is CheckStatus.OK
    assert "0 experiment(s)" in result.detail


def test_experiments_db_fails_when_registry_raises(monkeypatch, tmp_path):
    def _raise(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(checks, "ExperimentRegistry", _raise)
    result = checks.check_experiments_db()
    assert result.status is CheckStatus.FAIL
    assert "disk full" in result.detail


# ---------------------------------------------------------------------------
# checks.py -- not-yet-built capabilities
# ---------------------------------------------------------------------------

def test_broker_connection_is_not_implemented():
    result = checks.check_broker_connection()
    assert result.status is CheckStatus.NOT_IMPLEMENTED
    assert "Sprint 5" in result.detail


def test_api_keys_is_not_implemented():
    result = checks.check_api_keys()
    assert result.status is CheckStatus.NOT_IMPLEMENTED


# ---------------------------------------------------------------------------
# doctor.py -- aggregation and formatting
# ---------------------------------------------------------------------------

def test_format_report_all_ok_says_everything_healthy():
    results = [CheckResult("A", CheckStatus.OK), CheckResult("B", CheckStatus.OK)]
    report = doctor_module._format_report(results)
    assert "Everything Healthy" in report
    assert "not yet implemented" not in report


def test_format_report_with_not_implemented_still_says_healthy():
    results = [CheckResult("A", CheckStatus.OK), CheckResult("B", CheckStatus.NOT_IMPLEMENTED, "future work")]
    report = doctor_module._format_report(results)
    assert "Everything Healthy" in report
    assert "1 not yet implemented" in report
    assert "B" in report


def test_format_report_with_failure_lists_it_and_omits_healthy_claim():
    results = [CheckResult("A", CheckStatus.OK), CheckResult("B", CheckStatus.FAIL, "boom")]
    report = doctor_module._format_report(results)
    assert "Everything Healthy" not in report
    assert "1 check(s) failed: B" in report
    assert "(boom)" in report


def test_run_all_checks_converts_a_raising_check_into_a_failure(isolated_registry):
    @register_check("Explodes")
    def exploding_check() -> CheckResult:
        raise ValueError("kaboom")

    results = doctor_module._run_all_checks()
    assert len(results) == 1
    assert results[0].status is CheckStatus.FAIL
    assert "kaboom" in results[0].detail


def test_run_doctor_returns_1_on_any_failure(isolated_registry, capsys):
    @register_check("Broken")
    def broken() -> CheckResult:
        return CheckResult("Broken", CheckStatus.FAIL, "nope")

    exit_code = doctor_module.run_doctor()
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Broken" in captured.out


def test_run_doctor_returns_0_when_all_healthy(isolated_registry, capsys):
    @register_check("Fine")
    def fine() -> CheckResult:
        return CheckResult("Fine", CheckStatus.OK)

    exit_code = doctor_module.run_doctor()
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Everything Healthy" in captured.out


# ---------------------------------------------------------------------------
# __main__.py -- command dispatch
# ---------------------------------------------------------------------------

def test_main_dispatches_to_doctor(monkeypatch):
    called = []
    monkeypatch.setitem(main_module.COMMANDS, "doctor", lambda: called.append(True) or 0)
    exit_code = main(["doctor"])
    assert exit_code == 0
    assert called == [True]


def test_main_returns_2_for_unknown_command(capsys):
    exit_code = main(["frobnicate"])
    assert exit_code == 2
    assert "usage" in capsys.readouterr().out


def test_main_returns_2_for_no_args(capsys):
    exit_code = main([])
    assert exit_code == 2
