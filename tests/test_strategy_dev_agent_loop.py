"""Tests for the Strategy Development Agent's loop, budget enforcement,
and audit/provenance -- src/ai/agents/strategy_dev/dev_agent.py (Sprint
14 spec, sections 32-33, 37, 52, 55, 116-124, Phase 8). Every test here
uses `FakeLLMProvider` -- no network access anywhere in this file, and
`run_candidate`/`MarketDataService` are faked/monkeypatched so no real
subprocess or network fetch happens either.

Mirrors tests/test_ai_agent_loop.py's own structure and scenario
naming (Scenario A-G, per the Sprint 14 spec) since
`StrategyDevelopmentAgent._loop()` deliberately mirrors
`ResearchAgent._loop()`'s state machine (see dev_agent.py's module
docstring).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

pytest.importorskip("joblib")  # src.ai.agents.strategy_dev.tools -> ResearchTrialService ->
# src.ai.registry.ModelRegistry transitively requires joblib -- guarded before the heavy
# imports below, mirroring tests/test_ai_agent_loop.py's own convention.

from src.ai.agents.provider import FakeLLMProvider, LLMResponse, LLMToolCall, ProviderError
from src.ai.agents.strategy_dev import tools as tools_mod
from src.ai.agents.strategy_dev.dev_agent import StrategyDevelopmentAgent
from src.ai.agents.strategy_dev.models import CandidateStrategyStatus, DevAgentRunStatus
from src.ai.agents.strategy_dev.policy import StrategyDevelopmentAgentPolicy
from src.ai.agents.strategy_dev.runner import RunnerResult
from src.ai.agents.strategy_dev.tools import default_dev_tool_registry
from src.ai.agents.strategy_dev.workspace import CandidateRegistry, CandidateWorkspace
from src.data.base import Interval
from src.data.models import CandleDataset, SessionPolicy, ValidationReport
from src.experiments.registry import ExperimentRegistry
from src.research.trial_service import ResearchTrialService
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

# Fails validate_candidate_source: imports os, calls eval, no BaseStrategy base.
MALICIOUS_SOURCE = """\
import os


class Evil:
    def prepare(self, data):
        eval("1+1")
        return data

    def generate_signals(self, data):
        return []
"""


def _candles(n: int = 60) -> pd.DataFrame:
    closes = [100.0 + (i % 7) - 3 + i * 0.1 for i in range(n)]
    dates = pd.date_range("2024-01-01", periods=n, freq="D", name="timestamp", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
        },
        index=dates,
    )


class FakeMarketDataService:
    """Mirrors tests/test_ai_agent_loop.py's own fixture -- no network."""

    def __init__(self, candles=None) -> None:
        self._candles = candles if candles is not None else _candles()

    def get_dataset(self, symbol, start=None, end=None, interval=Interval.DAY_1, **kwargs):
        interval = Interval(interval)
        return CandleDataset(
            symbol=symbol,
            interval=interval,
            provider="fake",
            candles=self._candles,
            content_hash="fake-hash",
            validation_report=ValidationReport(symbol=symbol, interval=interval),
            session_policy=SessionPolicy.REGULAR,
            session_timezone="America/New_York",
            requested_start=start or date(2024, 1, 1),
            requested_end=end or date(2024, 3, 1),
            dataset_start=self._candles.index.min(),
            dataset_end=self._candles.index.max(),
        )


@pytest.fixture
def experiment_registry(tmp_path) -> ExperimentRegistry:
    return ExperimentRegistry(db_path=tmp_path / "experiments.db")


@pytest.fixture
def trial_service() -> ResearchTrialService:
    return ResearchTrialService(market_data_service=FakeMarketDataService())


@pytest.fixture
def workspace(tmp_path) -> CandidateWorkspace:
    return CandidateWorkspace(base_dir=tmp_path / "candidates")


@pytest.fixture
def candidate_registry(tmp_path) -> CandidateRegistry:
    return CandidateRegistry(base_dir=tmp_path / "candidates")


@pytest.fixture
def real_registry(experiment_registry, trial_service, workspace, candidate_registry):
    return default_dev_tool_registry(
        experiment_registry=experiment_registry,
        trial_service=trial_service,
        workspace=workspace,
        candidate_registry=candidate_registry,
    )


@pytest.fixture(autouse=True)
def _fake_market_data_service(monkeypatch):
    """`RunCandidateBacktestTool.execute()` constructs its own
    `MarketDataService()` directly (to fetch candles for the isolated
    candidate runner), independent of whatever market data service was
    injected into `ResearchTrialService` -- patch the class itself so
    no test in this file ever reaches a real data provider."""
    import src.data.service as data_service_mod

    monkeypatch.setattr(data_service_mod, "MarketDataService", FakeMarketDataService)


@pytest.fixture(autouse=True)
def _fake_run_candidate(monkeypatch):
    """No real subprocess in any test in this file -- the candidate
    runner's isolated-execution contract is `runner.py`'s own tests'
    job, not the agent loop's."""

    def _fake(source_path, candles, symbol, params, timeout=30):
        return RunnerResult(
            signals=(
                Signal(
                    timestamp=pd.Timestamp("2024-01-10", tz="UTC"),
                    symbol=symbol,
                    direction=SignalDirection.LONG,
                    confidence=0.6,
                    metadata={},
                ),
                Signal(
                    timestamp=pd.Timestamp("2024-02-10", tz="UTC"),
                    symbol=symbol,
                    direction=SignalDirection.FLAT,
                    confidence=0.6,
                    metadata={},
                ),
            )
        )

    monkeypatch.setattr(tools_mod, "run_candidate", _fake)


def final_answer(text: str) -> LLMResponse:
    return LLMResponse(text=text)


def tool_call(name: str, arguments: dict, call_id: str = "call-1") -> LLMResponse:
    return LLMResponse(text=None, tool_calls=[LLMToolCall(id=call_id, name=name, arguments=arguments)])


def _create_args(source=VALID_SOURCE, **overrides) -> dict:
    args = {
        "research_question": "does a vol filter reduce drawdown",
        "hypothesis": "EMA + vol filter reduces drawdown vs plain EMA cross",
        "strategy_name": "ema_vol_filter",
        "strategy_description": "EMA cross gated by a volatility regime filter",
        "entry_logic": "EMA fast crosses above slow",
        "exit_logic": "EMA fast crosses below slow",
        "indicator_dependencies": ["EMA"],
        "parameters": {"fast": 10, "slow": 30},
        "symbol": "QQQ",
        "interval": "15m",
        "signal_semantics": "LONG/FLAT only",
        "expected_behavior": "fewer whipsaws in high-vol regimes",
        "source": source,
    }
    args.update(overrides)
    return args


def _backtest_args(candidate_id: str, stage: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "stage": stage,
        "symbol": "QQQ",
        "interval": "15m",
        "start": "2024-01-01",
        "end": "2024-03-01",
    }


# ---------------------------------------------------------------------------
# Scenario A: sufficient existing strategy -- no candidate needed.
# ---------------------------------------------------------------------------


def test_scenario_a_agent_stops_early_when_no_candidate_is_needed(real_registry):
    provider = FakeLLMProvider(
        [final_answer("Observed: an existing strategy already covers this hypothesis; no candidate needed.")]
    )
    agent = StrategyDevelopmentAgent(provider=provider, tool_registry=real_registry, persist=False)
    run = agent.run("Is there already a strategy that does X?")
    assert run.status == DevAgentRunStatus.COMPLETED.value
    assert run.step_count == 1
    assert run.candidates_created_count == 0


# ---------------------------------------------------------------------------
# Scenario B/E: one candidate happy path through freeze and a final test.
# ---------------------------------------------------------------------------


def test_scenario_b_full_candidate_lifecycle_to_frozen_and_final_test(
    real_registry, candidate_registry, _fake_run_candidate, _fake_market_data_service
):
    # `_fake_run_candidate`/`_fake_market_data_service` are also
    # `autouse=True`; requesting them explicitly here is redundant
    # under real pytest but required for this repo's minimal sandbox
    # test runner (`/tmp/runner_all.py`), which does not implement
    # autouse fixture discovery -- harmless either way.
    create_args = _create_args()
    provider = FakeLLMProvider(
        [
            tool_call("create_candidate_strategy", create_args, "c1"),
            tool_call("validate_candidate", {"candidate_id": "__CID__"}, "c2"),
            tool_call("test_candidate", {"candidate_id": "__CID__"}, "c3"),
            tool_call("run_candidate_backtest", _backtest_args("__CID__", "development"), "c4"),
            tool_call("freeze_candidate", {"candidate_id": "__CID__"}, "c5"),
            tool_call("run_candidate_backtest", _backtest_args("__CID__", "final_test"), "c6"),
            tool_call("get_candidate_report", {"candidate_id": "__CID__"}, "c7"),
            final_answer("Observed: candidate frozen and evaluated out-of-sample; see report."),
        ]
    )
    # The candidate_id isn't known until create_candidate_strategy runs, so
    # patch each subsequent scripted call's arguments in place once it is.
    agent = _RewritingAgent(provider=provider, tool_registry=real_registry, persist=False)
    run = agent.run("Develop and evaluate a vol-filtered EMA candidate.")

    assert run.status == DevAgentRunStatus.COMPLETED.value
    assert run.candidates_created_count == 1
    assert run.validation_backtests_count == 1
    assert run.final_test_evaluations_count == 1
    assert len(run.candidate_ids) == 1
    candidate_id = run.candidate_ids[0]
    assert run.final_report.candidates_created == (candidate_id,)
    assert run.final_report.candidates_frozen == (candidate_id,)

    candidate = candidate_registry.get(candidate_id)
    assert candidate.status == CandidateStrategyStatus.REVIEW_REQUIRED
    assert candidate.final_test_evaluation is not None
    assert candidate.development_evaluation is not None
    # Full provenance recorded on the candidate, not just its name (Sprint 14 spec, section 23).
    assert candidate.agent_run_id == run.run_id
    assert candidate.provider == "fake"
    assert candidate.model == provider.model
    assert candidate.tool_schema_version == run.tool_schema_version
    assert candidate.policy_hash == run.policy_hash


class _RewritingAgent(StrategyDevelopmentAgent):
    """A tiny test seam: after each tool call, rewrite any later
    scripted `LLMToolCall.arguments["candidate_id"]` placeholder
    (`"__CID__"`) to the real, just-created candidate_id -- lets a
    fully scripted `FakeLLMProvider` conversation exercise a realistic
    multi-step lifecycle without a real LLM able to read prior tool
    output and decide the next call's arguments itself."""

    def _handle_tool_call(self, call, session):
        result = super()._handle_tool_call(call, session)
        if call.name == "create_candidate_strategy" and not result.is_error:
            candidate_id = result.data["candidate_id"]
            provider = self._provider
            for entry in provider._responses:
                response = entry() if callable(entry) else entry
                for tc in response.tool_calls:
                    if isinstance(tc.arguments, dict) and tc.arguments.get("candidate_id") == "__CID__":
                        tc.arguments["candidate_id"] = candidate_id
        return result


# ---------------------------------------------------------------------------
# Scenario C: candidate fails validation and is revised.
# ---------------------------------------------------------------------------


def test_scenario_c_candidate_fails_validation_and_is_revised(real_registry, candidate_registry):
    provider = FakeLLMProvider(
        [
            tool_call("create_candidate_strategy", _create_args(source=MALICIOUS_SOURCE), "c1"),
        ]
    )
    agent = _RevisionAgent(provider=provider, tool_registry=real_registry, persist=False)
    run = agent.run("Develop a candidate.")

    assert run.status == DevAgentRunStatus.COMPLETED.value
    assert run.candidates_created_count == 2
    assert run.candidate_revisions_count == 1
    assert len(run.candidate_ids) == 2

    first_id, second_id = run.candidate_ids
    assert candidate_registry.get(first_id).status == CandidateStrategyStatus.REJECTED
    assert candidate_registry.get(second_id).status == CandidateStrategyStatus.VALIDATED
    assert candidate_registry.get(second_id).parent_candidate_id == first_id


class _RevisionAgent(StrategyDevelopmentAgent):
    """Scripts a realistic revise-after-rejection sequence without a
    real LLM: after the first candidate is created, validate it; once
    validation reports REJECTED, create a revision with a valid source
    and a `parent_candidate_id` back-reference, then validate that one
    too, then stop."""

    def _loop(self, run_id, goal, session, tool_defs):
        first_call = LLMToolCall(id="c1", name="create_candidate_strategy", arguments=_create_args(source=MALICIOUS_SOURCE))
        first_result = self._handle_tool_call(first_call, session)
        first_id = first_result.data["candidate_id"]

        validate_first = LLMToolCall(id="c2", name="validate_candidate", arguments={"candidate_id": first_id})
        self._handle_tool_call(validate_first, session)

        second_call = LLMToolCall(
            id="c3",
            name="create_candidate_strategy",
            arguments=_create_args(
                strategy_name="ema_vol_filter_v2",
                parent_candidate_id=first_id,
                revision_reason="fixed forbidden import/eval flagged by validation",
            ),
        )
        second_result = self._handle_tool_call(second_call, session)
        second_id = second_result.data["candidate_id"]

        validate_second = LLMToolCall(id="c4", name="validate_candidate", arguments={"candidate_id": second_id})
        self._handle_tool_call(validate_second, session)

        return self._build_report(
            run_id, goal, "COMPLETED", "Observed: first candidate rejected, revision validated.", session
        )


# ---------------------------------------------------------------------------
# Scenario D: candidate budget exhausted -- agent stops honestly.
# ---------------------------------------------------------------------------


def test_scenario_d_max_candidates_budget_stops_the_run_honestly(real_registry):
    provider = FakeLLMProvider(
        [
            tool_call("create_candidate_strategy", _create_args(source=MALICIOUS_SOURCE, strategy_name="a"), "c1"),
            tool_call("create_candidate_strategy", _create_args(strategy_name="b", parent_candidate_id="whatever"), "c2"),
        ]
    )
    policy = StrategyDevelopmentAgentPolicy(max_candidates=1)
    agent = StrategyDevelopmentAgent(provider=provider, tool_registry=real_registry, policy=policy, persist=False)
    run = agent.run("Keep creating candidates until one works.")

    assert run.status == DevAgentRunStatus.BUDGET_EXHAUSTED.value
    assert run.candidates_created_count == 1
    assert run.final_report is not None
    assert run.final_report.limitations
    assert "budget" in run.final_report.limitations[0].lower()


def test_step_budget_exhaustion_terminates_a_repeating_loop(real_registry):
    provider = FakeLLMProvider([tool_call("list_indicators", {}) for _ in range(50)])
    policy = StrategyDevelopmentAgentPolicy(max_steps=3)
    agent = StrategyDevelopmentAgent(provider=provider, tool_registry=real_registry, policy=policy, persist=False)
    run = agent.run("Keep listing indicators forever.")
    assert run.status == DevAgentRunStatus.BUDGET_EXHAUSTED.value
    assert run.step_count == 3


# ---------------------------------------------------------------------------
# Scenario F: final test runs once; the candidate is immutable afterward.
# ---------------------------------------------------------------------------


def test_scenario_f_candidate_is_immutable_after_final_test(
    real_registry, candidate_registry, _fake_run_candidate, _fake_market_data_service
):
    provider = FakeLLMProvider(
        [
            tool_call("create_candidate_strategy", _create_args(), "c1"),
        ]
    )
    agent = _ImmutabilityAgent(provider=provider, tool_registry=real_registry, persist=False)
    run = agent.run("Develop and freeze one candidate, then try to keep tweaking it.")

    assert run.status == DevAgentRunStatus.COMPLETED.value
    assert run.final_test_evaluations_count == 1
    candidate_id = run.candidate_ids[0]
    assert candidate_registry.get(candidate_id).status == CandidateStrategyStatus.OUT_OF_SAMPLE_TESTED

    # The post-final-test mutation attempt recorded in run.tool_calls must
    # have been refused, not silently accepted.
    post_final_attempts = [
        c for c in run.tool_calls if c.tool_name == "run_candidate_backtest" and c.is_error
    ]
    assert post_final_attempts
    assert post_final_attempts[-1].error_code in ("NOT_READY", "TEST_LOCKED")


class _ImmutabilityAgent(StrategyDevelopmentAgent):
    def _loop(self, run_id, goal, session, tool_defs):
        create = LLMToolCall(id="c1", name="create_candidate_strategy", arguments=_create_args())
        candidate_id = self._handle_tool_call(create, session).data["candidate_id"]

        self._handle_tool_call(
            LLMToolCall(id="c2", name="validate_candidate", arguments={"candidate_id": candidate_id}), session
        )
        self._handle_tool_call(
            LLMToolCall(id="c3", name="test_candidate", arguments={"candidate_id": candidate_id}), session
        )
        self._handle_tool_call(
            LLMToolCall(
                id="c4", name="run_candidate_backtest", arguments=_backtest_args(candidate_id, "development")
            ),
            session,
        )
        self._handle_tool_call(
            LLMToolCall(id="c5", name="freeze_candidate", arguments={"candidate_id": candidate_id}), session
        )
        self._handle_tool_call(
            LLMToolCall(
                id="c6", name="run_candidate_backtest", arguments=_backtest_args(candidate_id, "final_test")
            ),
            session,
        )
        # The candidate is now OUT_OF_SAMPLE_TESTED -- a further backtest
        # request must be refused, never silently re-run.
        self._handle_tool_call(
            LLMToolCall(
                id="c7", name="run_candidate_backtest", arguments=_backtest_args(candidate_id, "development")
            ),
            session,
        )

        return self._build_report(
            run_id, goal, "COMPLETED", "Observed: candidate frozen and tested; further edits refused.", session
        )


# ---------------------------------------------------------------------------
# Provider / tool failure handling
# ---------------------------------------------------------------------------


def test_provider_failure_produces_failed_status_not_a_fabricated_success(real_registry):
    def raise_error():
        raise ProviderError("NETWORK_ERROR", "connection reset")

    provider = FakeLLMProvider([raise_error])
    agent = StrategyDevelopmentAgent(provider=provider, tool_registry=real_registry, persist=False)
    run = agent.run("Develop a candidate.")
    assert run.status == DevAgentRunStatus.FAILED.value
    assert "NETWORK_ERROR" in run.error
    assert run.final_report is None


def test_invalid_tool_arguments_are_structured_and_agent_continues(real_registry):
    provider = FakeLLMProvider(
        [
            tool_call("create_candidate_strategy", {}),  # missing every required field
            final_answer("Observed: the creation attempt failed schema validation."),
        ]
    )
    agent = StrategyDevelopmentAgent(provider=provider, tool_registry=real_registry, persist=False)
    run = agent.run("Develop a candidate.")
    assert run.status == DevAgentRunStatus.COMPLETED.value
    assert run.tool_calls[0].is_error
    assert run.tool_calls[0].error_code == "INVALID_ARGUMENTS"
    assert run.candidates_created_count == 0


def test_unknown_tool_request_is_rejected_structurally(real_registry):
    provider = FakeLLMProvider(
        [
            tool_call("promote_candidate", {"candidate_id": "whatever"}),
            final_answer("Observed: that action is not available to a strategy development agent."),
        ]
    )
    agent = StrategyDevelopmentAgent(provider=provider, tool_registry=real_registry, persist=False)
    run = agent.run("Promote your best candidate.")
    assert run.status == DevAgentRunStatus.COMPLETED.value
    assert run.tool_calls[0].is_error
    assert run.tool_calls[0].error_code == "UNKNOWN_TOOL"


# ---------------------------------------------------------------------------
# Provenance / audit
# ---------------------------------------------------------------------------


def test_run_records_full_provenance_and_budget_usage(real_registry):
    create_args = _create_args()
    provider = FakeLLMProvider(
        [
            tool_call("create_candidate_strategy", create_args, "c1"),
            final_answer("Observed: one candidate created."),
        ]
    )
    agent = StrategyDevelopmentAgent(provider=provider, tool_registry=real_registry, persist=False)
    run = agent.run("Create one candidate.")

    assert run.provider == "fake"
    assert run.model == provider.model
    assert run.system_prompt_version
    assert run.tool_schema_version
    assert run.policy_hash
    assert run.candidates_created_count == 1
    assert run.candidate_revisions_count == 0
    assert run.validation_backtests_count == 0
    assert run.final_test_evaluations_count == 0
    assert run.started_at.tzinfo is not None
    assert run.finished_at.tzinfo is not None


def test_agent_policy_hard_denies_dangerous_flags_regardless_of_tool_surface():
    with pytest.raises(ValueError):
        StrategyDevelopmentAgentPolicy(allow_live_trading=True)
    with pytest.raises(ValueError):
        StrategyDevelopmentAgentPolicy(allow_auto_promotion=True)


def test_default_registry_never_exposes_promotion_git_or_shell_tools(real_registry):
    names = {tool.name for tool in real_registry.all_tools()}
    forbidden_substrings = ("promote", "git", "shell", "exec", "subprocess", "bash")
    for name in names:
        for substring in forbidden_substrings:
            assert substring not in name, f"tool {name!r} looks like a forbidden capability"
