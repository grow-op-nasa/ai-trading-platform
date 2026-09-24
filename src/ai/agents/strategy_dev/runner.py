"""The controlled candidate runner -- src/ai/agents/strategy_dev/runner.py.

Sprint 14 spec, sections 8, 12-13, 29-30: the platform-owned subprocess
boundary between untrusted candidate source and everything else. The
agent never receives a shell/subprocess tool (`tools.py` has none); this
module is internal infrastructure `test_candidate`/`run_candidate_backtest`
call, never something the LLM invokes directly.

    run_candidate(source, candles, symbol, params)
        |
        v
    write candles to a temp JSON file (no pickle -- plain records)
        |
        v
    subprocess.run([sys.executable, _harness.py, source, candles, symbol, params],
                    env=<sanitized: PATH + PYTHONPATH only, no secrets>,
                    cwd=<repo root>, timeout=..., capture_output=True)
        |
        v
    parse stdout JSON -> list[Signal] | RunnerError

Explicit defense-in-depth, not a claim of perfect isolation (Sprint 14
spec, section 30): the subprocess still runs on the same machine with
the same filesystem and network stack as the parent process. What this
module *does* guarantee: the candidate process never sees
`ANTHROPIC_API_KEY` or any other parent-process environment variable
(the sanitized `env=` replaces the environment entirely, it does not
inherit and filter), never receives a working directory write grant
beyond the repo's own read access, and is killed outright if it runs
longer than `timeout` seconds. Combined with `safety.py`'s static
denial of network/filesystem/subprocess/dynamic-exec imports and
constructs *before* this module ever runs the code, this is the
research-grade execution boundary Sprint 14 asks for -- not a hardened
sandbox for genuinely hostile code.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from src.signals.models import Signal, SignalDirection
from src.strategies.base import Strategy

_HARNESS_PATH = Path(__file__).with_name("_harness.py")
_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_TIMEOUT_SECONDS = 30

__all__ = [
    "RunnerResult",
    "run_candidate",
    "PrecomputedSignalStrategy",
    "DEFAULT_TIMEOUT_SECONDS",
]


@dataclass(frozen=True)
class RunnerResult:
    """The outcome of one isolated candidate execution.

    Args:
        signals: the candidate's emitted `Signal` objects. Empty if
            `error` is set.
        error: `None` on success; otherwise a short, structured message
            (never a raw traceback with local filesystem paths --
            `_harness.py` only ever reports `f"{type(exc).__name__}:
            {exc}"`).
        timed_out: `True` if the candidate process was killed for
            exceeding `timeout` (Sprint 14 spec, section 29: "bound
            pathological candidates from hanging the agent").
    """

    signals: tuple[Signal, ...] = ()
    error: str | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out


def run_candidate(
    source_path: Path | str,
    candles: pd.DataFrame,
    symbol: str,
    params: dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> RunnerResult:
    """Execute the already-statically-validated candidate at
    `source_path` in an isolated subprocess against `candles`, and
    return the `Signal`s it produced.

    Callers (`tools.py`'s `test_candidate`/`run_candidate_backtest`)
    are responsible for having already run
    `safety.validate_candidate_source()` and refused to call this
    function at all if validation failed (Sprint 14 spec, section 9:
    "a candidate that fails validation must never execute").
    """
    with tempfile.TemporaryDirectory(prefix="candidate_runner_") as tmp:
        candles_path = Path(tmp) / "candles.json"
        _write_candles(candles, candles_path)

        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_REPO_ROOT)}
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(_HARNESS_PATH),
                    str(source_path),
                    str(candles_path),
                    symbol,
                    json.dumps(params),
                ],
                env=env,
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return RunnerResult(error=f"candidate execution exceeded {timeout}s timeout", timed_out=True)

        if completed.returncode != 0 and not completed.stdout.strip():
            stderr_tail = completed.stderr.strip()[-500:]
            return RunnerResult(error=f"candidate process exited {completed.returncode}: {stderr_tail}")

        try:
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return RunnerResult(error="candidate process produced no parseable output")

        if payload.get("error"):
            return RunnerResult(error=str(payload["error"]))

        signals = tuple(_signal_from_dict(raw) for raw in payload.get("signals", []))
        return RunnerResult(signals=signals)


def _write_candles(candles: pd.DataFrame, path: Path) -> None:
    frame = candles.reset_index()
    frame = frame.rename(columns={frame.columns[0]: "timestamp"})
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).astype(str)
    records = json.loads(frame.to_json(orient="records"))
    path.write_text(json.dumps(records), encoding="utf-8")


def _signal_from_dict(raw: dict) -> Signal:
    return Signal(
        timestamp=pd.Timestamp(raw["timestamp"]),
        symbol=raw["symbol"],
        direction=SignalDirection(raw["direction"]),
        confidence=raw["confidence"],
        metadata=raw.get("metadata", {}),
    )


class PrecomputedSignalStrategy:
    """A trusted, platform-owned `Strategy` adapter wrapping a fixed,
    already-generated `list[Signal]` (Sprint 14 spec, section 111: "do
    not add `if strategy is CandidateStrategy` inside core Backtester
    logic" -- so instead of teaching `PortfolioBacktestEngine` a second
    kind of strategy, this class *is* an ordinary `Strategy` from the
    engine's point of view).

    The untrusted candidate's own `.prepare()`/`.generate_signals()`
    already ran, safely, inside `run_candidate()`'s isolated subprocess
    (Sprint 14 spec, section 29); by the time a `PrecomputedSignalStrategy`
    reaches `Backtester.run_portfolio()`, only its plain, JSON-safe
    `Signal` output crosses back into the trusted process -- the
    engine never touches candidate source again.
    """

    def __init__(self, name: str, symbol: str, signals: list[Signal]) -> None:
        self._name = name
        self._symbol = symbol
        self._signals = list(signals)

    @property
    def name(self) -> str:
        return self._name

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def params(self) -> dict:
        return {}

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        return data

    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        return list(self._signals)


assert isinstance(PrecomputedSignalStrategy("x", "SPY", []), Strategy)
