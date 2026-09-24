"""Tests for the isolated candidate execution boundary --
src/ai/agents/strategy_dev/{runner,_harness}.py (Sprint 14 spec,
sections 8, 12-13, 29-30, Phase 5/11).

Two layers are tested separately:

  * `_harness.main()` in-process -- fast, no subprocess -- for the
    harness's own contract: usage errors, zero/multiple-class source,
    exceptions raised by candidate code, and non-JSON-safe metadata
    filtering. Calling `main()` directly (rather than through a real
    subprocess) is deliberate here: these are pure logic/parsing
    concerns of the harness script itself, and don't need process
    isolation to observe.

  * `run_candidate()` end-to-end through a *real* subprocess -- proving
    the actual isolation properties the module's docstring promises:
    the child process never inherits parent environment variables
    (secret-access-blocked), a pathological candidate is killed after
    `timeout` seconds rather than hanging the caller (timeout
    enforcement), and a candidate that raises partway through (invalid
    source, a runtime exception, or an attempted network call to a
    non-routable address) is reported back as a structured
    `RunnerResult.error`, never a crash or an unbounded hang.

None of this depends on `safety.py`'s static import allowlist --
`run_candidate()`'s own docstring is explicit that callers are
responsible for having already run `validate_candidate_source()`
first, and refusing to call this module at all if that fails.
Forbidden-import *blocking* (os/socket/subprocess/etc. at the AST
level, before anything ever executes) is this file's neighbor's job:
`tests/test_strategy_dev_safety.py`'s 47 tests already cover every
representative bypass attempt there. What's tested here is what
happens once already-validated-shaped code actually runs: correctness
of the execution contract, and safe, bounded failure when it
misbehaves at runtime despite passing static checks (or when a test
deliberately skips validation to probe the runner's own defenses in
isolation, as several tests below do).

No network, no LLM, no joblib/sklearn dependency -- `runner.py` and
`_harness.py` only import pandas, `src.signals.models`, and
`src.strategies.base`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

from src.ai.agents.strategy_dev import _harness
from src.ai.agents.strategy_dev.runner import (
    DEFAULT_TIMEOUT_SECONDS,
    PrecomputedSignalStrategy,
    RunnerResult,
    run_candidate,
)
from src.signals.models import Signal, SignalDirection

# ---------------------------------------------------------------------------
# Candidate source fixtures. Deliberately independent of BaseStrategy (which
# pulls in loguru) -- runner.py/_harness.py never require BaseStrategy, only
# the plain Strategy shape (name/prepare/generate_signals via structural
# typing), so these stay minimal and dependency-light.
# ---------------------------------------------------------------------------

MINIMAL_CANDIDATE_SOURCE = '''\
from src.signals.models import Signal, SignalDirection


class MinimalCandidate:
    def __init__(self, symbol, threshold=0.5):
        self._symbol = symbol
        self._threshold = threshold

    @property
    def name(self):
        return "minimal_candidate"

    def prepare(self, data):
        return data

    def generate_signals(self, data):
        signals = []
        for timestamp, row in data.iterrows():
            if row["close"] > self._threshold:
                signals.append(
                    Signal(
                        timestamp=timestamp,
                        symbol=self._symbol,
                        direction=SignalDirection.LONG,
                        confidence=0.9,
                        metadata={"close": float(row["close"])},
                    )
                )
                break
        return signals
'''

RAISING_CANDIDATE_SOURCE = '''\
class RaisingCandidate:
    def __init__(self, symbol):
        self._symbol = symbol

    def prepare(self, data):
        return data

    def generate_signals(self, data):
        raise ValueError("boom: candidate logic failed")
'''

ZERO_CLASS_SOURCE = '''\
def not_a_class(symbol):
    return None
'''

MULTIPLE_CLASS_SOURCE = '''\
class FirstCandidate:
    def __init__(self, symbol):
        self._symbol = symbol

    def prepare(self, data):
        return data

    def generate_signals(self, data):
        return []


class SecondCandidate:
    def __init__(self, symbol):
        self._symbol = symbol

    def prepare(self, data):
        return data

    def generate_signals(self, data):
        return []
'''

NON_JSON_SAFE_METADATA_SOURCE = '''\
from src.signals.models import Signal, SignalDirection


class MetadataCandidate:
    def __init__(self, symbol):
        self._symbol = symbol

    def prepare(self, data):
        return data

    def generate_signals(self, data):
        timestamp = data.index[0]
        return [
            Signal(
                timestamp=timestamp,
                symbol=self._symbol,
                direction=SignalDirection.LONG,
                confidence=0.5,
                metadata={
                    "reason": "safe string",
                    "count": 3,
                    "callback": lambda x: x,
                },
            )
        ]
'''

SECRET_PROBING_CANDIDATE_SOURCE = '''\
import os
from src.signals.models import Signal, SignalDirection


class SecretProbingCandidate:
    def __init__(self, symbol):
        self._symbol = symbol

    def prepare(self, data):
        return data

    def generate_signals(self, data):
        timestamp = data.index[0]
        return [
            Signal(
                timestamp=timestamp,
                symbol=self._symbol,
                direction=SignalDirection.LONG,
                confidence=0.5,
                metadata={
                    "api_key_seen": os.environ.get("ANTHROPIC_API_KEY", "MISSING"),
                    "path_seen": os.environ.get("PATH", "MISSING"),
                    "custom_var_seen": os.environ.get("SUPER_SECRET_TOKEN", "MISSING"),
                },
            )
        ]
'''

SLEEPING_CANDIDATE_SOURCE = '''\
import time
from src.signals.models import Signal, SignalDirection


class SleepingCandidate:
    def __init__(self, symbol):
        self._symbol = symbol

    def prepare(self, data):
        return data

    def generate_signals(self, data):
        time.sleep(10)
        return []
'''

NETWORK_ATTEMPT_CANDIDATE_SOURCE = '''\
import socket


class NetworkAttemptCandidate:
    def __init__(self, symbol):
        self._symbol = symbol

    def prepare(self, data):
        return data

    def generate_signals(self, data):
        # 192.0.2.0/24 is TEST-NET-1 (RFC 5737): reserved, never routable,
        # so this deterministically fails/times out regardless of the
        # host machine's actual network access -- no live network
        # dependency in this test.
        socket.create_connection(("192.0.2.1", 80), timeout=1)
        return []
'''


def _candles() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    return pd.DataFrame({"close": [0.1, 0.2, 0.6, 0.8, 0.9]}, index=idx)


def _write_candidate(tmp_path: Path, source: str, name: str = "candidate.py") -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def _write_candles_json(tmp_path: Path, records: list[dict], name: str = "candles.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _harness.main() -- in-process (fast, no subprocess needed for these).
# ---------------------------------------------------------------------------


def test_usage_error_with_wrong_argument_count(capsys):
    exit_code = _harness.main(["_harness.py", "only_one_extra_arg"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["signals"] == []
    assert "usage" in payload["error"]


def test_successful_execution_reports_signals_and_no_error(tmp_path, capsys):
    source_path = _write_candidate(tmp_path, MINIMAL_CANDIDATE_SOURCE)
    candles_path = _write_candles_json(
        tmp_path,
        [
            {"timestamp": "2024-01-01T00:00:00+00:00", "close": 0.1},
            {"timestamp": "2024-01-02T00:00:00+00:00", "close": 0.9},
        ],
    )

    exit_code = _harness.main(
        ["_harness.py", str(source_path), str(candles_path), "QQQ", "{}"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"] is None
    assert len(payload["signals"]) == 1
    assert payload["signals"][0]["symbol"] == "QQQ"
    assert payload["signals"][0]["direction"] == "LONG"


def test_constructor_params_are_forwarded_from_params_json(tmp_path, capsys):
    source_path = _write_candidate(tmp_path, MINIMAL_CANDIDATE_SOURCE)
    candles_path = _write_candles_json(
        tmp_path,
        [{"timestamp": "2024-01-01T00:00:00+00:00", "close": 0.6}],
    )

    # threshold=0.9 means the only candle (close=0.6) never crosses it.
    exit_code = _harness.main(
        ["_harness.py", str(source_path), str(candles_path), "QQQ", '{"threshold": 0.9}']
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"] is None
    assert payload["signals"] == []


def test_zero_classes_in_source_is_reported_as_a_structured_error(tmp_path, capsys):
    source_path = _write_candidate(tmp_path, ZERO_CLASS_SOURCE)
    candles_path = _write_candles_json(
        tmp_path, [{"timestamp": "2024-01-01T00:00:00+00:00", "close": 0.1}]
    )

    exit_code = _harness.main(
        ["_harness.py", str(source_path), str(candles_path), "QQQ", "{}"]
    )

    assert exit_code == 0  # harness itself completed normally; the error is in the payload
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["signals"] == []
    assert "RuntimeError" in payload["error"]
    assert "exactly one class" in payload["error"]


def test_multiple_classes_in_source_is_reported_as_a_structured_error(tmp_path, capsys):
    source_path = _write_candidate(tmp_path, MULTIPLE_CLASS_SOURCE)
    candles_path = _write_candles_json(
        tmp_path, [{"timestamp": "2024-01-01T00:00:00+00:00", "close": 0.1}]
    )

    exit_code = _harness.main(
        ["_harness.py", str(source_path), str(candles_path), "QQQ", "{}"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["signals"] == []
    assert "RuntimeError" in payload["error"]
    assert "FirstCandidate" in payload["error"] and "SecondCandidate" in payload["error"]


def test_exception_raised_by_candidate_code_is_caught_and_reported_not_propagated(tmp_path, capsys):
    source_path = _write_candidate(tmp_path, RAISING_CANDIDATE_SOURCE)
    candles_path = _write_candles_json(
        tmp_path, [{"timestamp": "2024-01-01T00:00:00+00:00", "close": 0.1}]
    )

    # main() must not raise -- untrusted candidate code failing is reported,
    # never allowed to propagate out of the harness.
    exit_code = _harness.main(
        ["_harness.py", str(source_path), str(candles_path), "QQQ", "{}"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["signals"] == []
    assert payload["error"] == "ValueError: boom: candidate logic failed"


def test_missing_source_file_is_caught_and_reported_not_propagated(tmp_path, capsys):
    missing_path = tmp_path / "does_not_exist.py"
    candles_path = _write_candles_json(
        tmp_path, [{"timestamp": "2024-01-01T00:00:00+00:00", "close": 0.1}]
    )

    exit_code = _harness.main(
        ["_harness.py", str(missing_path), str(candles_path), "QQQ", "{}"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["signals"] == []
    assert "FileNotFoundError" in payload["error"]


def test_non_json_safe_metadata_values_are_dropped_not_fatal(tmp_path, capsys):
    source_path = _write_candidate(tmp_path, NON_JSON_SAFE_METADATA_SOURCE)
    candles_path = _write_candles_json(
        tmp_path, [{"timestamp": "2024-01-01T00:00:00+00:00", "close": 0.1}]
    )

    exit_code = _harness.main(
        ["_harness.py", str(source_path), str(candles_path), "QQQ", "{}"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"] is None
    metadata = payload["signals"][0]["metadata"]
    assert metadata == {"reason": "safe string", "count": 3}
    assert "callback" not in metadata


# ---------------------------------------------------------------------------
# run_candidate() -- real subprocess, proving the actual isolation contract.
# ---------------------------------------------------------------------------


def test_run_candidate_executes_a_real_subprocess_and_returns_signals(tmp_path):
    source_path = _write_candidate(tmp_path, MINIMAL_CANDIDATE_SOURCE)

    result = run_candidate(source_path, _candles(), "QQQ", {"threshold": 0.5})

    assert result.ok
    assert result.error is None
    assert not result.timed_out
    assert len(result.signals) == 1
    signal = result.signals[0]
    assert isinstance(signal, Signal)
    assert signal.symbol == "QQQ"
    assert signal.direction is SignalDirection.LONG


def test_run_candidate_forwards_params_through_a_real_subprocess(tmp_path):
    source_path = _write_candidate(tmp_path, MINIMAL_CANDIDATE_SOURCE)

    # threshold above every close in _candles() (max 0.9) -> never triggers.
    result = run_candidate(source_path, _candles(), "QQQ", {"threshold": 0.95})

    assert result.ok
    assert result.signals == ()


def test_run_candidate_reports_runtime_exception_without_crashing_the_caller(tmp_path):
    source_path = _write_candidate(tmp_path, RAISING_CANDIDATE_SOURCE)

    result = run_candidate(source_path, _candles(), "QQQ", {})

    assert not result.ok
    assert not result.timed_out
    assert result.signals == ()
    assert "ValueError" in result.error
    assert "boom" in result.error


def test_run_candidate_reports_invalid_source_without_crashing_the_caller(tmp_path):
    # Deliberately malformed Python -- proves a syntax error in candidate
    # source is reported as a RunnerResult.error, not an unhandled crash of
    # the parent process (this bypasses safety.py on purpose, to probe the
    # runner/harness's own defenses independent of the static validator).
    source_path = _write_candidate(tmp_path, "def broken(:\n    pass\n")

    result = run_candidate(source_path, _candles(), "QQQ", {})

    assert not result.ok
    assert not result.timed_out
    assert result.error is not None


@pytest.mark.skipif(
    sys.platform == "win32", reason="subprocess timeout semantics differ on Windows"
)
def test_run_candidate_kills_a_pathological_candidate_after_the_timeout(tmp_path):
    source_path = _write_candidate(tmp_path, SLEEPING_CANDIDATE_SOURCE)

    started = time.monotonic()
    result = run_candidate(source_path, _candles(), "QQQ", {}, timeout=2)
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert not result.ok
    assert result.signals == ()
    assert "timeout" in result.error.lower()
    # Killed near the requested timeout, not left to run the full 10s sleep.
    assert elapsed < 8


def test_run_candidate_sanitized_environment_never_exposes_parent_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret-do-not-leak")
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "another-secret-value")
    source_path = _write_candidate(tmp_path, SECRET_PROBING_CANDIDATE_SOURCE)

    result = run_candidate(source_path, _candles(), "QQQ", {})

    assert result.ok, result.error
    assert len(result.signals) == 1
    metadata = result.signals[0].metadata
    # The child process must never inherit the parent's environment --
    # run_candidate() replaces env= entirely rather than filtering it.
    assert metadata["api_key_seen"] == "MISSING"
    assert metadata["custom_var_seen"] == "MISSING"
    # Only the minimal PATH the runner itself grants is visible.
    assert metadata["path_seen"] == "/usr/bin:/bin"


def test_run_candidate_network_attempt_to_a_non_routable_address_fails_closed(tmp_path):
    # Demonstrates defense-in-depth, not a network firewall: run_candidate()
    # does not sandbox the OS network stack (module docstring is explicit
    # about this), so this proves the *harness* safely reports any resulting
    # exception as a structured error rather than hanging or crashing --
    # the actual denial of network-capable imports happens statically, in
    # safety.py, before code ever reaches this subprocess.
    source_path = _write_candidate(tmp_path, NETWORK_ATTEMPT_CANDIDATE_SOURCE)

    result = run_candidate(source_path, _candles(), "QQQ", {}, timeout=10)

    assert not result.ok
    assert result.signals == ()
    assert result.error is not None


def test_run_candidate_default_timeout_constant_is_a_reasonable_positive_number():
    assert isinstance(DEFAULT_TIMEOUT_SECONDS, int)
    assert 0 < DEFAULT_TIMEOUT_SECONDS <= 300


# ---------------------------------------------------------------------------
# RunnerResult / PrecomputedSignalStrategy -- plain unit tests, no subprocess.
# ---------------------------------------------------------------------------


def test_runner_result_ok_is_true_only_with_no_error_and_no_timeout():
    assert RunnerResult(signals=(), error=None, timed_out=False).ok
    assert not RunnerResult(signals=(), error="boom", timed_out=False).ok
    assert not RunnerResult(signals=(), error=None, timed_out=True).ok
    assert not RunnerResult(signals=(), error="boom", timed_out=True).ok


def test_precomputed_signal_strategy_wraps_fixed_signals_and_satisfies_strategy_protocol():
    from src.strategies.base import Strategy

    signal = Signal(
        timestamp=pd.Timestamp("2024-01-01", tz="UTC"),
        symbol="QQQ",
        direction=SignalDirection.LONG,
        confidence=0.7,
    )
    strategy = PrecomputedSignalStrategy(name="candidate_x", symbol="QQQ", signals=[signal])

    assert isinstance(strategy, Strategy)
    assert strategy.name == "candidate_x"
    assert strategy.symbol == "QQQ"
    assert strategy.params == {}

    data = pd.DataFrame({"close": [1.0]})
    assert strategy.prepare(data) is data
    produced = strategy.generate_signals(data)
    assert produced == [signal]
    # generate_signals() returns a fresh list each call, not a shared
    # reference to internal state.
    assert produced is not strategy._signals


def test_precomputed_signal_strategy_defensively_copies_its_signal_list_at_construction():
    signals = [
        Signal(
            timestamp=pd.Timestamp("2024-01-01", tz="UTC"),
            symbol="QQQ",
            direction=SignalDirection.LONG,
            confidence=0.5,
        )
    ]
    strategy = PrecomputedSignalStrategy(name="x", symbol="QQQ", signals=signals)
    signals.append(
        Signal(
            timestamp=pd.Timestamp("2024-01-02", tz="UTC"),
            symbol="QQQ",
            direction=SignalDirection.FLAT,
            confidence=0.5,
        )
    )

    # Mutating the caller's original list after construction must not affect
    # what the strategy returns.
    assert len(strategy.generate_signals(pd.DataFrame())) == 1
