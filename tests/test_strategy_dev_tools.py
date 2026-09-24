"""Unit tests for the Strategy Development Agent's tool surface --
src/ai/agents/strategy_dev/tools.py.

Every tool that would otherwise touch the isolated subprocess runner
(`run_candidate`) or a real `MarketDataService`/`ResearchTrialService`
is exercised against fakes/monkeypatches -- these are unit tests for
tool-level contract enforcement (permission gating, schema validation,
state-machine guards, the test-set lock), not an end-to-end proof that
the real subprocess/backtest pipeline works. That real-machine proof
belongs to a smoke test, not this file (Sprint 14 spec, sections
120-121).
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

pytest.importorskip("joblib")  # src.ai.agents.strategy_dev.tools -> ResearchTrialService ->
# src.ai.registry.ModelRegistry transitively requires joblib -- guard placed before the
# heavy imports below, mirroring tests/test_ai_agent_loop.py's own convention, so this
# whole file is cleanly skipped (not reported as an import error) wherever joblib isn't
# installed.

from src.ai.agents.strategy_dev.models import (
    CandidateEvaluation,
    CandidateStrategySpec,
    CandidateStrategyStatus,
)
from src.ai.agents.strategy_dev.runner import RunnerResult
from src.ai.agents.strategy_dev.workspace import CandidateRegistry, CandidateWorkspace
from src.ai.agents.strategy_dev import tools as tools_mod
from src.ai.agents.strategy_dev.tools import (
    CompareCandidateToBaselineTool,
    CreateCandidateStrategyTool,
    FreezeCandidateTool,
    GetCandidateReportTool,
    InspectCandidateTool,
    ListIndicatorsTool,
    RunCandidateBacktestTool,
    TestCandidateTool,
    ValidateCandidateTool,
    default_dev_tool_registry,
)
from src.ai.agents.strategy_dev.policy import StrategyDevelopmentAgentPolicy
from src.ai.agents.tools import ToolRegistry
from src.signals.models import Signal, SignalDirection


VALID_SOURCE = '''\
from src.strategies.sdk import BaseStrategy
from src.signals.models import SignalDirection


class EmaVolFilterCandidate(BaseStrategy):
    """A candidate that filters EMA crosses by realized volatility."""

    def __init__(self, symbol: str, fast: int = 10, slow: int = 30):
        super().__init__(name="ema_vol_filter_candidate", symbol=symbol)
        self._fast = fast
        self._slow = slow

    def prepare(self, data):
        self.require_columns(data, "close")
        out = data.copy()
        out["ema_fast"] = self.indicator(data, "EMA", period=self._fast)
        out["ema_slow"] = self.indicator(data, "EMA", period=self._slow)
        return out

    def generate_signals(self, data):
        signals = []
        in_position = False
        for timestamp, row in data.iterrows():
            if pd.isna(row["ema_fast"]) or pd.isna(row["ema_slow"]):
                continue
            crossed_up = row["ema_fast"] > row["ema_slow"]
            if crossed_up and not in_position:
                signals.append(self.emit_signal(timestamp, SignalDirection.LONG, confidence=0.6))
                in_position = True
            elif not crossed_up and in_position:
                signals.append(self.emit_signal(timestamp, SignalDirection.FLAT, confidence=0.6))
                in_position = False
        return signals
'''

# Fails validate_candidate_source: imports os, calls eval, no BaseStrategy.
MALICIOUS_SOURCE = """\
import os


class Evil:
    def prepare(self, data):
        eval("1+1")
        return data

    def generate_signals(self, data):
        return []
"""


def _spec(**overrides) -> CandidateStrategySpec:
    defaults = dict(
        research_question="does a vol filter reduce drawdown",
        hypothesis="EMA + vol filter reduces drawdown vs plain EMA cross",
        strategy_name="ema_vol_filter",
        strategy_description="EMA cross gated by a volatility regime filter",
        entry_logic="EMA fast crosses above slow",
        exit_logic="EMA fast crosses below slow",
        indicator_dependencies=("EMA",),
        parameters={"fast": 10, "slow": 30},
        symbol="QQQ",
        interval="15m",
        signal_semantics="LONG/FLAT only",
        expected_behavior="fewer whipsaws in high-vol regimes",
    )
    defaults.update(overrides)
    return CandidateStrategySpec(**defaults)


def _create_args(**overrides) -> dict:
    spec = _spec()
    args = {
        "research_question": spec.research_question,
        "hypothesis": spec.hypothesis,
        "strategy_name": spec.strategy_name,
        "strategy_description": spec.strategy_description,
        "entry_logic": spec.entry_logic,
        "exit_logic": spec.exit_logic,
        "indicator_dependencies": list(spec.indicator_dependencies),
        "parameters": spec.parameters,
        "symbol": spec.symbol,
        "interval": spec.interval,
        "signal_semantics": spec.signal_semantics,
        "expected_behavior": spec.expected_behavior,
        "source": VALID_SOURCE,
    }
    args.update(overrides)
    return args


@pytest.fixture
def workspace(tmp_path):
    return CandidateWorkspace(base_dir=tmp_path / "candidates")


@pytest.fixture
def registry(tmp_path):
    return CandidateRegistry(base_dir=tmp_path / "candidates")


@pytest.fixture
def run_context():
    return {
        "run_id": "run-abc",
        "provider": "anthropic",
        "model": "claude-x",
        "system_prompt_version": "v1",
        "tool_schema_version": "hash1",
        "policy_hash": "hash2",
    }


def _make_candidate(registry, workspace, run_context, source=VALID_SOURCE, status=None):
    tool = CreateCandidateStrategyTool(workspace, registry, run_context)
    result = tool.execute(_create_args(source=source))
    assert result.status == "ok", result.error_message
    candidate_id = result.data["candidate_id"]
    if status is not None:
        for step in _lifecycle_path(status):
            registry.transition(candidate_id, step)
    return candidate_id


def _lifecycle_path(target: CandidateStrategyStatus) -> list[CandidateStrategyStatus]:
    order = [
        CandidateStrategyStatus.VALIDATING,
        CandidateStrategyStatus.VALIDATED,
        CandidateStrategyStatus.DEVELOPMENT_TESTED,
        CandidateStrategyStatus.FROZEN,
        CandidateStrategyStatus.OUT_OF_SAMPLE_TESTED,
        CandidateStrategyStatus.REVIEW_REQUIRED,
    ]
    return order[: order.index(target) + 1]


# --------------------------------------------------------------------------
# ListIndicatorsTool
# --------------------------------------------------------------------------


def test_list_indicators_returns_engine_names():
    tool = ListIndicatorsTool()
    result = tool.execute({})
    assert result.status == "ok"
    assert "EMA" in result.data["indicators"]


# --------------------------------------------------------------------------
# CreateCandidateStrategyTool
# --------------------------------------------------------------------------


def test_create_candidate_happy_path(workspace, registry, run_context):
    tool = CreateCandidateStrategyTool(workspace, registry, run_context)
    result = tool.execute(_create_args())
    assert result.status == "ok"
    assert result.data["status"] == CandidateStrategyStatus.DRAFT.value
    candidate = registry.get(result.data["candidate_id"])
    assert candidate is not None
    assert candidate.agent_run_id == "run-abc"
    assert workspace.read_source(candidate.candidate_id) == VALID_SOURCE


def test_create_candidate_rejects_syntax_error(workspace, registry, run_context):
    tool = CreateCandidateStrategyTool(workspace, registry, run_context)
    result = tool.execute(_create_args(source="def broken(:\n"))
    assert result.is_error
    assert result.error_code == "SYNTAX_ERROR"
    assert registry.list_ids() == []


def test_create_candidate_rejects_unknown_interval(workspace, registry, run_context):
    tool = CreateCandidateStrategyTool(workspace, registry, run_context)
    result = tool.execute(_create_args(interval="not-a-real-interval"))
    assert result.is_error
    assert result.error_code == "INVALID_ARGUMENTS"


def test_create_candidate_rejects_source_too_large(workspace, registry, run_context):
    tool = CreateCandidateStrategyTool(workspace, registry, run_context)
    huge_source = VALID_SOURCE + ("\n# padding" * 5000)
    result = tool.execute(_create_args(source=huge_source))
    assert result.is_error
    assert result.error_code == "SOURCE_TOO_LARGE"


def test_create_candidate_rejects_exact_duplicate(workspace, registry, run_context):
    tool = CreateCandidateStrategyTool(workspace, registry, run_context)
    first = tool.execute(_create_args())
    assert first.status == "ok"
    second = tool.execute(_create_args())
    assert second.is_error
    assert second.error_code == "DUPLICATE_CANDIDATE"
    assert len(registry.list_ids()) == 1


def test_create_candidate_allows_different_spec_same_source(workspace, registry, run_context):
    tool = CreateCandidateStrategyTool(workspace, registry, run_context)
    first = tool.execute(_create_args())
    assert first.status == "ok"
    second = tool.execute(_create_args(strategy_name="a_different_name"))
    assert second.status == "ok"
    assert len(registry.list_ids()) == 2


# --------------------------------------------------------------------------
# InspectCandidateTool
# --------------------------------------------------------------------------


def test_inspect_candidate_not_found(registry):
    tool = InspectCandidateTool(registry)
    result = tool.execute({"candidate_id": "nope"})
    assert result.is_error
    assert result.error_code == "NOT_FOUND"


def test_inspect_candidate_returns_compact_metadata_no_source(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context)
    tool = InspectCandidateTool(registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.status == "ok"
    assert result.data["candidate_id"] == candidate_id
    assert "source" not in result.data
    assert result.data["has_final_test_evaluation"] is False


# --------------------------------------------------------------------------
# ValidateCandidateTool
# --------------------------------------------------------------------------


def test_validate_candidate_passes_valid_source(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context)
    tool = ValidateCandidateTool(workspace, registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.status == "ok"
    assert result.data["passed"] is True
    assert registry.get(candidate_id).status == CandidateStrategyStatus.VALIDATED


def test_validate_candidate_rejects_malicious_source(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, source=MALICIOUS_SOURCE)
    tool = ValidateCandidateTool(workspace, registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.status == "ok"  # the tool call succeeds; the candidate fails
    assert result.data["passed"] is False
    assert result.data["failed_checks"]
    candidate = registry.get(candidate_id)
    assert candidate.status == CandidateStrategyStatus.REJECTED
    assert candidate.rejection_reason


def test_validate_candidate_wrong_state(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.VALIDATED)
    tool = ValidateCandidateTool(workspace, registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.is_error
    assert result.error_code == "INVALID_STATE"


def test_validate_candidate_not_found(workspace, registry):
    tool = ValidateCandidateTool(workspace, registry)
    result = tool.execute({"candidate_id": "nope"})
    assert result.is_error
    assert result.error_code == "NOT_FOUND"


# --------------------------------------------------------------------------
# TestCandidateTool
# --------------------------------------------------------------------------


def _fake_signal(direction=SignalDirection.LONG):
    return Signal(
        timestamp=pd.Timestamp("2023-01-05", tz="UTC"),
        symbol="QQQ",
        direction=direction,
        confidence=0.6,
        metadata={},
    )


def test_test_candidate_requires_validated_state(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context)  # still DRAFT
    tool = TestCandidateTool(workspace, registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.is_error
    assert result.error_code == "NOT_VALIDATED"


def test_test_candidate_happy_path_transitions_to_development_tested(
    monkeypatch, workspace, registry, run_context
):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.VALIDATED)
    monkeypatch.setattr(
        tools_mod,
        "run_candidate",
        lambda *a, **k: RunnerResult(signals=(_fake_signal(SignalDirection.LONG), _fake_signal(SignalDirection.FLAT))),
    )
    tool = TestCandidateTool(workspace, registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.status == "ok"
    assert result.data["passed"] is True
    assert registry.get(candidate_id).status == CandidateStrategyStatus.DEVELOPMENT_TESTED


def test_test_candidate_reports_runner_failure_without_transition(
    monkeypatch, workspace, registry, run_context
):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.VALIDATED)
    monkeypatch.setattr(
        tools_mod, "run_candidate", lambda *a, **k: RunnerResult(error="ModuleNotFoundError: no network")
    )
    tool = TestCandidateTool(workspace, registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.status == "ok"
    assert result.data["passed"] is False
    assert registry.get(candidate_id).status == CandidateStrategyStatus.VALIDATED  # unchanged


def test_test_candidate_rejects_non_sparse_signals(monkeypatch, workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.VALIDATED)
    # 120 signals for a 120-row fixture is not sparse.
    many_signals = tuple(_fake_signal() for _ in range(120))
    monkeypatch.setattr(tools_mod, "run_candidate", lambda *a, **k: RunnerResult(signals=many_signals))
    tool = TestCandidateTool(workspace, registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.data["passed"] is False
    assert registry.get(candidate_id).status == CandidateStrategyStatus.VALIDATED


# --------------------------------------------------------------------------
# RunCandidateBacktestTool -- the test-set lock enforcement point
# --------------------------------------------------------------------------


class _FakeMetric:
    def __init__(self, value=0.0, status="COMPUTED", reason=None):
        self.value = value
        self.status = SimpleNamespace(value=status)
        self.reason = reason


def _fake_analytics(trade_count=3):
    return SimpleNamespace(
        total_pnl=_FakeMetric(100.0),
        total_return=_FakeMetric(0.05),
        sharpe_ratio=_FakeMetric(1.2),
        max_drawdown=_FakeMetric(-0.03),
        win_rate=_FakeMetric(0.6),
        trade_count=trade_count,
        profit_factor=_FakeMetric(1.8),
        expectancy=_FakeMetric(0.01),
    )


def _fake_outcome(stage_trade_count=3, risk_config=None):
    spec = SimpleNamespace(
        dataset_fingerprint="fp-123",
        dataset_start=pd.Timestamp("2024-01-01", tz="UTC"),
        dataset_end=pd.Timestamp("2024-03-01", tz="UTC"),
        risk_config=risk_config or {"max_position_pct": 0.1},
        backtest_config={"execution": {"timing": "SIGNAL_BAR_CLOSE"}},
    )
    result = SimpleNamespace(trades=[object()] * stage_trade_count)
    return SimpleNamespace(
        trial_id="trial-xyz", result=result, spec=spec, analytics=_fake_analytics(stage_trade_count)
    )


class _FakeTrialService:
    def __init__(self, outcome=None, raise_exc=None):
        self._outcome = outcome or _fake_outcome()
        self._raise = raise_exc
        self.calls: list[dict] = []

    def run_trial_with_strategy(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return self._outcome


class _FakeDataset:
    def __init__(self, candles):
        self.candles = candles


def _patch_market_data(monkeypatch, candles):
    import src.data.service as data_service_mod

    class _FakeMarketDataService:
        def get_dataset(self, symbol, start, end, interval):
            return _FakeDataset(candles)

    monkeypatch.setattr(data_service_mod, "MarketDataService", _FakeMarketDataService)


def _backtest_args(candidate_id, stage):
    return {
        "candidate_id": candidate_id,
        "stage": stage,
        "symbol": "QQQ",
        "interval": "15m",
        "start": "2024-01-01",
        "end": "2024-03-01",
    }


def test_run_candidate_backtest_development_happy_path(monkeypatch, workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.VALIDATED)
    monkeypatch.setattr(tools_mod, "run_candidate", lambda *a, **k: RunnerResult(signals=(_fake_signal(),)))
    _patch_market_data(monkeypatch, _synthetic_candles())
    trials = _FakeTrialService()
    tool = RunCandidateBacktestTool(workspace, registry, trials)

    result = tool.execute(_backtest_args(candidate_id, "development"))

    assert result.status == "ok", result.error_message
    assert len(trials.calls) == 1
    candidate = registry.get(candidate_id)
    assert candidate.development_evaluation is not None
    assert candidate.development_evaluation.trade_count == 3


def test_run_candidate_backtest_final_test_locked_before_freeze(monkeypatch, workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.DEVELOPMENT_TESTED)

    def _boom(*a, **k):
        raise AssertionError("run_candidate must not be called when the test lock is active")

    monkeypatch.setattr(tools_mod, "run_candidate", _boom)
    trials = _FakeTrialService()
    tool = RunCandidateBacktestTool(workspace, registry, trials)

    result = tool.execute(_backtest_args(candidate_id, "final_test"))

    assert result.is_error
    assert result.error_code == "TEST_LOCKED"
    assert trials.calls == []  # no backtest was ever run
    assert registry.get(candidate_id).final_test_evaluation is None


def test_run_candidate_backtest_final_test_runs_once_then_locks(monkeypatch, workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.FROZEN)
    monkeypatch.setattr(tools_mod, "run_candidate", lambda *a, **k: RunnerResult(signals=(_fake_signal(),)))
    _patch_market_data(monkeypatch, _synthetic_candles())
    trials = _FakeTrialService()
    tool = RunCandidateBacktestTool(workspace, registry, trials)

    first = tool.execute(_backtest_args(candidate_id, "final_test"))
    assert first.status == "ok", first.error_message
    candidate = registry.get(candidate_id)
    assert candidate.status == CandidateStrategyStatus.OUT_OF_SAMPLE_TESTED
    assert candidate.final_test_evaluation is not None

    second = tool.execute(_backtest_args(candidate_id, "final_test"))
    assert second.is_error
    # After the first final test, status has already moved on to
    # OUT_OF_SAMPLE_TESTED, so the tool's own "must be FROZEN" guard is
    # what catches a second attempt (TEST_LOCKED) -- it never even
    # reaches the "already have an evaluation" check (TEST_ALREADY_RUN,
    # exercised directly at the registry layer below). Either way, the
    # net effect the test-set lock requires holds: the final test never
    # runs a second time.
    assert second.error_code == "TEST_LOCKED"
    assert len(trials.calls) == 1  # the second attempt never ran a backtest

    # The complementary storage-level guarantee (CandidateRegistry
    # itself refuses a second final-test evaluation, independent of
    # the tool's own status check) -- Sprint 14 spec's defense-in-depth
    # for the test-set lock.
    from src.ai.agents.strategy_dev.workspace import CandidateImmutableError

    with pytest.raises(CandidateImmutableError):
        registry.record_evaluation(candidate_id, "final_test", candidate.final_test_evaluation)


def test_run_candidate_backtest_development_stage_requires_validated_or_tested(
    workspace, registry, run_context
):
    candidate_id = _make_candidate(registry, workspace, run_context)  # DRAFT
    trials = _FakeTrialService()
    tool = RunCandidateBacktestTool(workspace, registry, trials)
    result = tool.execute(_backtest_args(candidate_id, "development"))
    assert result.is_error
    assert result.error_code == "NOT_READY"


def test_run_candidate_backtest_invalid_date_range(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.VALIDATED)
    trials = _FakeTrialService()
    tool = RunCandidateBacktestTool(workspace, registry, trials)
    args = _backtest_args(candidate_id, "development")
    args["start"] = "not-a-date"
    result = tool.execute(args)
    assert result.is_error
    assert result.error_code == "INVALID_DATE_RANGE"


def test_run_candidate_backtest_candidate_execution_failure(monkeypatch, workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.VALIDATED)
    monkeypatch.setattr(tools_mod, "run_candidate", lambda *a, **k: RunnerResult(error="candidate crashed"))
    _patch_market_data(monkeypatch, _synthetic_candles())
    trials = _FakeTrialService()
    tool = RunCandidateBacktestTool(workspace, registry, trials)
    result = tool.execute(_backtest_args(candidate_id, "development"))
    assert result.is_error
    assert result.error_code == "CANDIDATE_EXECUTION_FAILED"


def _synthetic_candles():
    import numpy as np

    n = 60
    rng = np.random.default_rng(seed=7)
    closes = 100.0 + np.cumsum(rng.normal(0, 1, n))
    dates = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC", name="timestamp")
    return pd.DataFrame(
        {"open": closes, "high": closes + 1, "low": closes - 1, "close": closes, "volume": [1000.0] * n},
        index=dates,
    )


# --------------------------------------------------------------------------
# CompareCandidateToBaselineTool
# --------------------------------------------------------------------------


class _FakeExperimentRegistry:
    def __init__(self, spec=None):
        self._spec = spec

    def get_spec(self, experiment_id):
        return self._spec


def test_compare_candidate_no_evidence(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context)  # no evaluation yet
    tool = CompareCandidateToBaselineTool(registry, _FakeExperimentRegistry())
    result = tool.execute({"candidate_id": candidate_id, "baseline_experiment_id": 1})
    assert result.is_error
    assert result.error_code == "NO_EVIDENCE"


def test_compare_candidate_flags_risk_config_mismatch(monkeypatch, workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.VALIDATED)
    evaluation = CandidateEvaluation(
        trial_id="t1",
        stage="validation",
        dataset_fingerprint="fp",
        dataset_start="2024-01-01",
        dataset_end="2024-03-01",
        risk_config={"max_position_pct": 0.1},
        execution_config={"timing": "SIGNAL_BAR_CLOSE"},
        analytics={},
        trade_count=4,
    )
    registry.record_evaluation(candidate_id, "validation", evaluation)

    baseline_spec = SimpleNamespace(
        symbol="QQQ",
        risk_config={"max_position_pct": 0.2},  # deliberately different
        backtest_config={"execution": {"timing": "SIGNAL_BAR_CLOSE"}},
    )
    experiments = _FakeExperimentRegistry(spec=baseline_spec)

    monkeypatch.setattr(
        tools_mod.AnalyticsService, "analyze_experiment", lambda self, reg, exp_id: _fake_analytics(5)
    )
    tool = CompareCandidateToBaselineTool(registry, experiments)
    result = tool.execute({"candidate_id": candidate_id, "baseline_experiment_id": 1})

    assert result.status == "ok"
    assert any("risk configuration differs" in w for w in result.data["warnings"])


def test_compare_candidate_baseline_not_found(monkeypatch, workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.VALIDATED)
    evaluation = CandidateEvaluation(
        trial_id="t1", stage="validation", dataset_fingerprint="fp", dataset_start="2024-01-01",
        dataset_end="2024-03-01", risk_config={}, execution_config={}, analytics={}, trade_count=1,
    )
    registry.record_evaluation(candidate_id, "validation", evaluation)
    monkeypatch.setattr(tools_mod.AnalyticsService, "analyze_experiment", lambda self, reg, exp_id: None)
    tool = CompareCandidateToBaselineTool(registry, _FakeExperimentRegistry())
    result = tool.execute({"candidate_id": candidate_id, "baseline_experiment_id": 999})
    assert result.is_error
    assert result.error_code == "NOT_FOUND"


# --------------------------------------------------------------------------
# FreezeCandidateTool
# --------------------------------------------------------------------------


def test_freeze_candidate_happy_path(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.DEVELOPMENT_TESTED)
    tool = FreezeCandidateTool(registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.status == "ok"
    assert registry.get(candidate_id).status == CandidateStrategyStatus.FROZEN


def test_freeze_candidate_rejects_premature_freeze(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context)  # DRAFT
    tool = FreezeCandidateTool(registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.is_error
    assert result.error_code == "INVALID_STATE"


def test_freeze_candidate_not_found(registry):
    tool = FreezeCandidateTool(registry)
    result = tool.execute({"candidate_id": "nope"})
    assert result.is_error
    assert result.error_code == "NOT_FOUND"


def test_freeze_candidate_is_one_way(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.FROZEN)
    tool = FreezeCandidateTool(registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.is_error
    assert result.error_code == "INVALID_STATE"


# --------------------------------------------------------------------------
# GetCandidateReportTool
# --------------------------------------------------------------------------


def test_get_candidate_report_transitions_out_of_sample_tested_to_review_required(
    workspace, registry, run_context
):
    candidate_id = _make_candidate(
        registry, workspace, run_context, status=CandidateStrategyStatus.OUT_OF_SAMPLE_TESTED
    )
    tool = GetCandidateReportTool(registry)
    result = tool.execute({"candidate_id": candidate_id})
    assert result.status == "ok"
    assert result.data["status"] == CandidateStrategyStatus.REVIEW_REQUIRED.value
    assert registry.get(candidate_id).status == CandidateStrategyStatus.REVIEW_REQUIRED


def test_get_candidate_report_never_includes_a_single_quality_score(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context)
    tool = GetCandidateReportTool(registry)
    result = tool.execute({"candidate_id": candidate_id})
    forbidden_keys = {"quality_score", "score", "rating", "grade"}
    assert forbidden_keys.isdisjoint(result.data.keys())


# --------------------------------------------------------------------------
# default_dev_tool_registry() -- wiring, permission gating, schema validation
# --------------------------------------------------------------------------


def test_default_registry_wires_all_fourteen_tools(tmp_path):
    from src.experiments.registry import ExperimentRegistry
    from src.research.trial_service import ResearchTrialService

    registry = default_dev_tool_registry(
        experiment_registry=ExperimentRegistry(db_path=tmp_path / "experiments.db"),
        trial_service=ResearchTrialService(),
        workspace=CandidateWorkspace(base_dir=tmp_path / "candidates"),
        candidate_registry=CandidateRegistry(base_dir=tmp_path / "candidates"),
    )
    names = {tool.name for tool in registry.all_tools()}
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
    assert len(names) == 14


def test_tool_registry_policy_gates_freeze_when_disabled(workspace, registry, run_context):
    candidate_id = _make_candidate(registry, workspace, run_context, status=CandidateStrategyStatus.DEVELOPMENT_TESTED)
    tool_registry = ToolRegistry([FreezeCandidateTool(registry)])
    policy = StrategyDevelopmentAgentPolicy(allow_candidate_freeze=False)
    result = tool_registry.execute("freeze_candidate", {"candidate_id": candidate_id}, policy)
    assert result.is_error
    assert result.error_code == "POLICY_REJECTED"
    assert registry.get(candidate_id).status == CandidateStrategyStatus.DEVELOPMENT_TESTED


def test_tool_registry_hides_forbidden_tool_from_definitions(registry):
    tool_registry = ToolRegistry([FreezeCandidateTool(registry)])
    policy = StrategyDevelopmentAgentPolicy(allow_candidate_freeze=False)
    assert tool_registry.allowed_definitions(policy) == []


def test_tool_registry_schema_validation_rejects_missing_field(workspace, registry, run_context):
    tool_registry = ToolRegistry([ValidateCandidateTool(workspace, registry)])
    policy = StrategyDevelopmentAgentPolicy()
    result = tool_registry.execute("validate_candidate", {}, policy)
    assert result.is_error
    assert result.error_code == "INVALID_ARGUMENTS"


def test_tool_registry_unknown_tool(registry):
    tool_registry = ToolRegistry([])
    policy = StrategyDevelopmentAgentPolicy()
    result = tool_registry.execute("not_a_real_tool", {}, policy)
    assert result.is_error
    assert result.error_code == "UNKNOWN_TOOL"
