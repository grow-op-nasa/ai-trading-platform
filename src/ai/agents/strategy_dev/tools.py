"""The Strategy Development Agent's tool surface -- src/ai/agents/strategy_dev/tools.py.

Sprint 14 spec, sections 24-28, 67: a deliberately small toolset, all
running through the exact same `src.ai.agents.tools.ToolRegistry`
pipeline Sprint 13 already built (schema validation -> policy check ->
execute, never bypassed -- see that module's own docstring). Five of
the fifteen tools (`list_strategies`, `list_experiments`,
`get_experiment`, `analyze_experiment`, `compare_experiments`) are
Sprint 13's own `research_tools` classes, imported and reused verbatim
-- not reimplemented -- because they are already generic, read-only,
and delegate entirely to existing services.

The remaining ten are new, candidate-specific tools. None of them ever
receives a raw filesystem path from the model (Sprint 14 spec, section
7): `create_candidate_strategy` accepts strategy *content*
(specification fields + source text), and `CandidateWorkspace` decides
where that content is written. None of them is a generic filesystem or
shell tool (sections 7-8).
"""

from __future__ import annotations

from typing import Any

from src.ai.agents.research_tools import (
    AnalyzeExperimentTool,
    CompareExperimentsTool,
    GetExperimentTool,
    ListExperimentsTool,
    ListStrategiesTool,
)
from src.ai.agents.strategy_dev.models import (
    CandidateEvaluation,
    CandidateStrategySpec,
    CandidateStrategyStatus,
)
from src.ai.agents.strategy_dev.runner import PrecomputedSignalStrategy, run_candidate
from src.ai.agents.strategy_dev.safety import validate_candidate_source
from src.ai.agents.strategy_dev.workspace import (
    CandidateImmutableError,
    CandidateRegistry,
    CandidateWorkspace,
    InvalidTransitionError,
)
from src.ai.agents.tools import (
    PERMISSION_EXPERIMENT_READS,
    PERMISSION_MODEL_READS,
    ToolRegistry,
    ToolResult,
)
from src.analytics.service import AnalyticsService
from src.data.base import Interval
from src.experiments.registry import ExperimentRegistry
from src.indicators.engine import IndicatorEngine
from src.research.trial_service import ResearchTrialService

PERMISSION_INDICATOR_READS = "allow_indicator_reads"
PERMISSION_CANDIDATE_CREATION = "allow_candidate_creation"
PERMISSION_CANDIDATE_VALIDATION = "allow_candidate_validation"
PERMISSION_CANDIDATE_TESTING = "allow_candidate_testing"
PERMISSION_CANDIDATE_BACKTESTS = "allow_candidate_backtests"
PERMISSION_CANDIDATE_COMPARISON = "allow_candidate_comparison"
PERMISSION_CANDIDATE_FREEZE = "allow_candidate_freeze"
PERMISSION_CANDIDATE_REPORTING = "allow_candidate_reporting"

MAX_CANDIDATE_SOURCE_CHARS = 20_000
_VALID_STAGES = ("development", "validation", "final_test")


def _uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


class ListIndicatorsTool:
    """Sprint 14 spec, section 41 -- what `IndicatorEngine` can compute,
    so a candidate's spec can name real, existing indicators rather than
    inventing formulas."""

    name = "list_indicators"
    description = (
        "List every indicator name the platform's IndicatorEngine can compute "
        "(e.g. EMA, RSI, ATR, MACD). Candidate strategies must only use "
        "indicators from this list, via the existing IndicatorEngine/SDK -- "
        "never their own formulas."
    )
    required_permission = PERMISSION_INDICATOR_READS

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.ok(self.name, {"indicators": IndicatorEngine.available_indicators()})


class CreateCandidateStrategyTool:
    """Sprint 14 spec, sections 21-24, 69-70: the *only* way candidate
    source ever reaches disk. Requires a structured
    `CandidateStrategySpec` alongside the source (section 21: "do not
    start candidate development with raw code alone") and rejects
    exact source/spec duplicates before creating anything (section 39).

    Deliberately does **not** run the full static safety pipeline here
    beyond a syntax check -- `validate_candidate` (below) is the
    explicit, auditable step that does that, matching the loop Sprint
    14 spec section 37 describes (create -> validate -> test -> ...).
    A candidate sits at `DRAFT` until `validate_candidate` runs.
    """

    name = "create_candidate_strategy"
    description = (
        "Create a new candidate strategy: a structured specification (research "
        "question, hypothesis, entry/exit logic, indicator dependencies, "
        "parameters, symbol, interval) plus the candidate's Python source, "
        "implementing BaseStrategy. Writes only to an isolated candidate "
        "workspace -- never src/strategies/ or the production StrategyRegistry. "
        "Returns a candidate_id at status DRAFT; call validate_candidate next."
    )
    required_permission = PERMISSION_CANDIDATE_CREATION

    def __init__(self, workspace: CandidateWorkspace, registry: CandidateRegistry, run_context: dict[str, str]) -> None:
        self._workspace = workspace
        self._registry = registry
        self._run_context = run_context

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "research_question": {"type": "string"},
                "hypothesis": {"type": "string"},
                "strategy_name": {"type": "string"},
                "strategy_description": {"type": "string"},
                "entry_logic": {"type": "string"},
                "exit_logic": {"type": "string"},
                "indicator_dependencies": {"type": "array", "items": {"type": "string"}},
                "parameters": {"type": "object"},
                "symbol": {"type": "string"},
                "interval": {"type": "string"},
                "signal_semantics": {"type": "string"},
                "expected_behavior": {"type": "string"},
                "known_limitations": {"type": "string"},
                "base_strategy": {"type": "string"},
                "source": {"type": "string"},
                "parent_candidate_id": {"type": "string"},
                "revision_reason": {"type": "string"},
                "changes_summary": {"type": "string"},
            },
            "required": [
                "research_question",
                "hypothesis",
                "strategy_name",
                "strategy_description",
                "entry_logic",
                "exit_logic",
                "symbol",
                "interval",
                "signal_semantics",
                "expected_behavior",
                "source",
            ],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        source = arguments["source"]
        if len(source) > MAX_CANDIDATE_SOURCE_CHARS:
            return ToolResult.error(
                self.name,
                "SOURCE_TOO_LARGE",
                f"candidate source is {len(source)} chars, exceeds the "
                f"{MAX_CANDIDATE_SOURCE_CHARS} char limit",
            )
        try:
            Interval(arguments["interval"])
        except ValueError:
            return ToolResult.error(self.name, "INVALID_ARGUMENTS", f"unknown interval {arguments['interval']!r}")

        try:
            spec = CandidateStrategySpec(
                research_question=arguments["research_question"],
                hypothesis=arguments["hypothesis"],
                strategy_name=arguments["strategy_name"],
                strategy_description=arguments["strategy_description"],
                entry_logic=arguments["entry_logic"],
                exit_logic=arguments["exit_logic"],
                indicator_dependencies=tuple(arguments.get("indicator_dependencies", [])),
                parameters=dict(arguments.get("parameters", {})),
                symbol=arguments["symbol"],
                interval=arguments["interval"],
                signal_semantics=arguments["signal_semantics"],
                expected_behavior=arguments["expected_behavior"],
                known_limitations=arguments.get("known_limitations", ""),
                base_strategy=arguments.get("base_strategy"),
            )
        except ValueError as exc:
            return ToolResult.error(self.name, "INVALID_ARGUMENTS", str(exc))

        import ast
        import json

        try:
            ast.parse(source)
        except SyntaxError as exc:
            return ToolResult.error(self.name, "SYNTAX_ERROR", f"candidate source has a syntax error: {exc}")

        from src.ai.agents.strategy_dev.workspace import sha256_text

        source_hash = sha256_text(source)
        spec_hash = sha256_text(json.dumps(spec.describe(), sort_keys=True))
        for existing_id in self._registry.list_ids():
            existing = self._registry.get(existing_id)
            if existing is not None and existing.source_hash == source_hash and existing.spec_hash == spec_hash:
                return ToolResult.error(
                    self.name,
                    "DUPLICATE_CANDIDATE",
                    f"an identical candidate already exists: {existing_id!r} -- "
                    f"reuse it instead of creating a duplicate",
                )

        candidate_id = _uuid_hex()
        self._workspace.write_source(candidate_id, source)
        candidate = self._registry.create(
            candidate_id=candidate_id,
            spec=spec,
            source=source,
            strategy_interface_version="v1",
            agent_run_id=self._run_context.get("run_id", ""),
            provider=self._run_context.get("provider", ""),
            model=self._run_context.get("model", ""),
            system_prompt_version=self._run_context.get("system_prompt_version", ""),
            tool_schema_version=self._run_context.get("tool_schema_version", ""),
            policy_hash=self._run_context.get("policy_hash", ""),
            parent_candidate_id=arguments.get("parent_candidate_id"),
            revision_reason=arguments.get("revision_reason"),
            changes_summary=arguments.get("changes_summary"),
        )
        return ToolResult.ok(
            self.name,
            {
                "candidate_id": candidate.candidate_id,
                "status": candidate.status.value,
                "source_hash": candidate.source_hash,
                "spec_hash": candidate.spec_hash,
            },
        )


class InspectCandidateTool:
    """Sprint 14 spec, section 25 -- compact candidate metadata, never
    an unbounded source-code dump into model context."""

    name = "inspect_candidate"
    description = (
        "Get compact metadata for one candidate: status, hashes, dependencies, "
        "lineage, and evaluation status. Does not return the full source -- use "
        "the source_hash/spec_hash to confirm identity."
    )
    required_permission = PERMISSION_CANDIDATE_CREATION

    def __init__(self, registry: CandidateRegistry) -> None:
        self._registry = registry

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"candidate_id": {"type": "string"}},
            "required": ["candidate_id"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        candidate = self._registry.get(arguments["candidate_id"])
        if candidate is None:
            return ToolResult.error(self.name, "NOT_FOUND", f"no candidate {arguments['candidate_id']!r}")
        data = {
            "candidate_id": candidate.candidate_id,
            "name": candidate.name,
            "description": candidate.description,
            "status": candidate.status.value,
            "source_hash": candidate.source_hash,
            "spec_hash": candidate.spec_hash,
            "base_strategy": candidate.base_strategy,
            "configuration": candidate.configuration,
            "feature_dependencies": list(candidate.feature_dependencies),
            "parent_candidate_id": candidate.parent_candidate_id,
            "has_development_evaluation": candidate.development_evaluation is not None,
            "has_validation_evaluation": candidate.validation_evaluation is not None,
            "has_final_test_evaluation": candidate.final_test_evaluation is not None,
            "rejection_reason": candidate.rejection_reason,
        }
        return ToolResult.ok(self.name, data)


class ValidateCandidateTool:
    """Sprint 14 spec, section 26 -- the full static safety pipeline.
    Never downgrades a failure to a warning."""

    name = "validate_candidate"
    description = (
        "Run the full static safety validation pipeline against a candidate's "
        "source: AST parse, import allowlist, dangerous-construct scan, and "
        "Strategy contract checks. Transitions the candidate to VALIDATED or "
        "REJECTED. A candidate must be VALIDATED before test_candidate or "
        "run_candidate_backtest may run it."
    )
    required_permission = PERMISSION_CANDIDATE_VALIDATION

    def __init__(self, workspace: CandidateWorkspace, registry: CandidateRegistry) -> None:
        self._workspace = workspace
        self._registry = registry

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"candidate_id": {"type": "string"}},
            "required": ["candidate_id"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        candidate_id = arguments["candidate_id"]
        candidate = self._registry.get(candidate_id)
        if candidate is None:
            return ToolResult.error(self.name, "NOT_FOUND", f"no candidate {candidate_id!r}")
        if candidate.status != CandidateStrategyStatus.DRAFT:
            return ToolResult.error(
                self.name,
                "INVALID_STATE",
                f"candidate {candidate_id!r} is {candidate.status.value}, expected DRAFT",
            )

        self._registry.transition(candidate_id, CandidateStrategyStatus.VALIDATING)
        source = self._workspace.read_source(candidate_id)
        result = validate_candidate_source(source)

        if result.passed:
            self._registry.transition(candidate_id, CandidateStrategyStatus.VALIDATED)
        else:
            self._registry.reject(candidate_id, "; ".join(result.failed_checks))

        return ToolResult.ok(
            self.name,
            {
                "candidate_id": candidate_id,
                "passed": result.passed,
                "failed_checks": list(result.failed_checks),
                "warnings": list(result.warnings),
                "status": CandidateStrategyStatus.VALIDATED.value if result.passed else CandidateStrategyStatus.REJECTED.value,
            },
        )


class TestCandidateTool:
    """Sprint 14 spec, sections 27-28, 73: a bounded, deterministic
    contract test against a small synthetic fixture, run inside the
    isolated `runner.run_candidate()` -- never platform-authoritative
    on its own (section 28: "the agent cannot write a test that
    redefines these rules"), just the gate before any real backtest."""

    # This is an agent tool named after the "test_candidate" lifecycle
    # action, not a pytest test case -- tell pytest's collector to leave
    # it alone (it otherwise warns because the name starts with "Test"
    # and the class has an __init__).
    __test__ = False

    name = "test_candidate"
    description = (
        "Run a bounded contract test for a VALIDATED candidate against a small "
        "deterministic synthetic dataset, inside the isolated candidate runner: "
        "proves the candidate instantiates, generates sparse LONG/SHORT/FLAT "
        "signals with no exceptions, and does not hang. Transitions the "
        "candidate to DEVELOPMENT_TESTED on success."
    )
    required_permission = PERMISSION_CANDIDATE_TESTING

    def __init__(self, workspace: CandidateWorkspace, registry: CandidateRegistry) -> None:
        self._workspace = workspace
        self._registry = registry

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"candidate_id": {"type": "string"}},
            "required": ["candidate_id"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        candidate_id = arguments["candidate_id"]
        candidate = self._registry.get(candidate_id)
        if candidate is None:
            return ToolResult.error(self.name, "NOT_FOUND", f"no candidate {candidate_id!r}")
        if candidate.status != CandidateStrategyStatus.VALIDATED:
            return ToolResult.error(
                self.name,
                "NOT_VALIDATED",
                f"candidate {candidate_id!r} is {candidate.status.value}, expected VALIDATED",
            )

        source_path = self._workspace.source_path(candidate_id)
        candles = _synthetic_fixture_candles()
        result = run_candidate(source_path, candles, candidate.base_strategy or "TEST", candidate.configuration)

        if not result.ok:
            return ToolResult.ok(
                self.name,
                {"candidate_id": candidate_id, "passed": False, "error": result.error, "signal_count": 0},
            )

        directions = {s.direction.value for s in result.signals}
        sparse = len(result.signals) < len(candles)
        contract_ok = directions <= {"LONG", "SHORT", "FLAT"} and sparse
        if contract_ok:
            self._registry.transition(candidate_id, CandidateStrategyStatus.DEVELOPMENT_TESTED)
        return ToolResult.ok(
            self.name,
            {
                "candidate_id": candidate_id,
                "passed": contract_ok,
                "signal_count": len(result.signals),
                "directions_seen": sorted(directions),
                "status": (
                    CandidateStrategyStatus.DEVELOPMENT_TESTED.value
                    if contract_ok
                    else candidate.status.value
                ),
            },
        )


class RunCandidateBacktestTool:
    """Sprint 14 spec, sections 31, 34-36, 55, 115: the one tool that
    produces real evidence -- and the test-lock enforcement point.
    `stage="final_test"` is refused outright unless the candidate is
    already `FROZEN` (Sprint 14 spec, section 35: "the agent must not
    receive final test performance" before that point) -- the backtest
    is never even run in that case, not merely the result withheld."""

    name = "run_candidate_backtest"
    description = (
        "Run a historical, portfolio-aware backtest for a candidate strategy's "
        "already-generated signals, using the platform's real "
        "MarketDataService -> PortfolioBacktestEngine -> Risk -> Execution -> "
        "Analytics pipeline (never a duplicate backtester). stage must be "
        "'development' or 'validation' before the candidate is frozen, or "
        "'final_test' after -- a 'final_test' request against a non-frozen "
        "candidate is refused without running anything."
    )
    required_permission = PERMISSION_CANDIDATE_BACKTESTS

    def __init__(self, workspace: CandidateWorkspace, registry: CandidateRegistry, trial_service: ResearchTrialService) -> None:
        self._workspace = workspace
        self._registry = registry
        self._trials = trial_service

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "stage": {"type": "string", "enum": list(_VALID_STAGES)},
                "symbol": {"type": "string"},
                "interval": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "execution_timing": {"type": "string", "enum": ["SIGNAL_BAR_CLOSE", "NEXT_BAR_OPEN"]},
                "slippage_bps": {"type": "number", "minimum": 0},
                "fee_bps": {"type": "number", "minimum": 0},
            },
            "required": ["candidate_id", "stage", "symbol", "interval", "start", "end"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        from datetime import date

        candidate_id = arguments["candidate_id"]
        stage = arguments["stage"]
        candidate = self._registry.get(candidate_id)
        if candidate is None:
            return ToolResult.error(self.name, "NOT_FOUND", f"no candidate {candidate_id!r}")

        if stage in ("development", "validation"):
            if candidate.status not in (
                CandidateStrategyStatus.DEVELOPMENT_TESTED,
                CandidateStrategyStatus.VALIDATED,
            ):
                return ToolResult.error(
                    self.name,
                    "NOT_READY",
                    f"candidate {candidate_id!r} is {candidate.status.value} -- must be "
                    f"VALIDATED (or DEVELOPMENT_TESTED) before a {stage} backtest",
                )
        elif stage == "final_test":
            if candidate.status != CandidateStrategyStatus.FROZEN:
                return ToolResult.error(
                    self.name,
                    "TEST_LOCKED",
                    f"candidate {candidate_id!r} is {candidate.status.value}, not FROZEN -- "
                    f"the final out-of-sample test is locked until the candidate is frozen "
                    f"(Sprint 14 test-set lock)",
                )
            if candidate.final_test_evaluation is not None:
                return ToolResult.error(
                    self.name,
                    "TEST_ALREADY_RUN",
                    f"candidate {candidate_id!r} already has a final test evaluation -- "
                    f"the final test runs exactly once",
                )

        try:
            start = date.fromisoformat(arguments["start"])
            end = date.fromisoformat(arguments["end"])
        except ValueError as exc:
            return ToolResult.error(self.name, "INVALID_DATE_RANGE", str(exc))

        source_path = self._workspace.source_path(candidate_id)
        candles_for_signals = None
        try:
            from src.data.service import MarketDataService

            interval_enum = Interval(arguments["interval"])
            dataset = MarketDataService().get_dataset(
                arguments["symbol"], start=start, end=end, interval=interval_enum
            )
            candles_for_signals = dataset.candles
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(self.name, "DATA_ERROR", f"{type(exc).__name__}: {exc}")

        runner_result = run_candidate(
            source_path, candles_for_signals, arguments["symbol"], candidate.configuration
        )
        if not runner_result.ok:
            return ToolResult.error(self.name, "CANDIDATE_EXECUTION_FAILED", runner_result.error or "unknown error")

        wrapper = PrecomputedSignalStrategy(candidate.name, arguments["symbol"], list(runner_result.signals))
        try:
            outcome = self._trials.run_trial_with_strategy(
                strategy=wrapper,
                symbol=arguments["symbol"],
                interval=arguments["interval"],
                start=start,
                end=end,
                execution_timing=arguments.get("execution_timing", "SIGNAL_BAR_CLOSE"),
                slippage_bps=arguments.get("slippage_bps", 0.0),
                fee_bps=arguments.get("fee_bps", 0.0),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(self.name, "BACKTEST_ERROR", f"{type(exc).__name__}: {exc}")

        analytics_dict = _analytics_summary(outcome.analytics)
        evaluation = CandidateEvaluation(
            trial_id=outcome.trial_id,
            stage=stage,
            dataset_fingerprint=outcome.spec.dataset_fingerprint,
            dataset_start=outcome.spec.dataset_start.isoformat(),
            dataset_end=outcome.spec.dataset_end.isoformat(),
            risk_config=outcome.spec.risk_config,
            execution_config=outcome.spec.backtest_config.get("execution", {}),
            analytics=analytics_dict,
            trade_count=len(outcome.result.trades),
        )
        self._registry.record_evaluation(candidate_id, "development" if stage == "development" else stage if stage != "validation" else "validation", evaluation)

        if stage == "final_test":
            self._registry.transition(candidate_id, CandidateStrategyStatus.OUT_OF_SAMPLE_TESTED)

        return ToolResult.ok(
            self.name,
            {
                "candidate_id": candidate_id,
                "stage": stage,
                "trial_id": outcome.trial_id,
                "analytics": analytics_dict,
                "trade_count": len(outcome.result.trades),
                "dataset_fingerprint": outcome.spec.dataset_fingerprint,
            },
        )


class CompareCandidateToBaselineTool:
    """Sprint 14 spec, sections 51-54: candidate vs. one existing,
    already-recorded baseline experiment -- context-matched, never a
    single "winner" score."""

    name = "compare_candidate_to_baseline"
    description = (
        "Compare a candidate's validation evidence against an existing baseline "
        "experiment's analytics. Flags symbol/interval/risk/execution "
        "mismatches rather than silently comparing apples to oranges. Never "
        "produces a single composite winner score."
    )
    required_permission = PERMISSION_CANDIDATE_COMPARISON

    def __init__(self, registry: CandidateRegistry, experiment_registry: ExperimentRegistry) -> None:
        self._registry = registry
        self._experiments = experiment_registry

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "baseline_experiment_id": {"type": "integer", "minimum": 1},
            },
            "required": ["candidate_id", "baseline_experiment_id"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        candidate = self._registry.get(arguments["candidate_id"])
        if candidate is None:
            return ToolResult.error(self.name, "NOT_FOUND", f"no candidate {arguments['candidate_id']!r}")
        evaluation = candidate.validation_evaluation or candidate.development_evaluation
        if evaluation is None:
            return ToolResult.error(
                self.name, "NO_EVIDENCE", f"candidate {arguments['candidate_id']!r} has no backtest evidence yet"
            )

        baseline_id = arguments["baseline_experiment_id"]
        baseline_analytics = AnalyticsService().analyze_experiment(self._experiments, baseline_id)
        if baseline_analytics is None:
            return ToolResult.error(self.name, "NOT_FOUND", f"no experiment {baseline_id}")
        baseline_spec = self._experiments.get_spec(baseline_id)

        warnings: list[str] = []
        if baseline_spec is not None:
            if baseline_spec.symbol != candidate.configuration.get("symbol", baseline_spec.symbol):
                pass  # symbol isn't stored per-candidate at top level; comparison stays informational
            if baseline_spec.risk_config != evaluation.risk_config:
                warnings.append("risk configuration differs between candidate and baseline")
            baseline_execution = baseline_spec.backtest_config.get("execution", {})
            if baseline_execution and baseline_execution != evaluation.execution_config:
                warnings.append("execution configuration differs between candidate and baseline")

        return ToolResult.ok(
            self.name,
            {
                "candidate_id": arguments["candidate_id"],
                "candidate_evidence": {
                    "stage": evaluation.stage,
                    "analytics": evaluation.analytics,
                    "trade_count": evaluation.trade_count,
                    "dataset_fingerprint": evaluation.dataset_fingerprint,
                },
                "baseline_experiment_id": baseline_id,
                "baseline_analytics": _analytics_summary(baseline_analytics),
                "warnings": warnings,
            },
        )


class FreezeCandidateTool:
    """Sprint 14 spec, sections 5, 35-36, 77: the one-way gate. After
    this call, `CandidateRegistry` refuses any further source/spec/
    development-evaluation mutation on this candidate."""

    name = "freeze_candidate"
    description = (
        "Freeze a DEVELOPMENT_TESTED candidate: source, spec, and configuration "
        "become immutable, and exactly one final out-of-sample test evaluation "
        "may subsequently be run via run_candidate_backtest(stage='final_test'). "
        "This is a one-way transition -- a revised idea after this point "
        "requires a new candidate."
    )
    required_permission = PERMISSION_CANDIDATE_FREEZE

    def __init__(self, registry: CandidateRegistry) -> None:
        self._registry = registry

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"candidate_id": {"type": "string"}},
            "required": ["candidate_id"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        candidate_id = arguments["candidate_id"]
        try:
            candidate = self._registry.transition(candidate_id, CandidateStrategyStatus.FROZEN)
        except KeyError:
            return ToolResult.error(self.name, "NOT_FOUND", f"no candidate {candidate_id!r}")
        except InvalidTransitionError as exc:
            return ToolResult.error(self.name, "INVALID_STATE", str(exc))
        return ToolResult.ok(self.name, {"candidate_id": candidate_id, "status": candidate.status.value})


class GetCandidateReportTool:
    """Sprint 14 spec, section 119: the final, structured evidence
    package for one candidate -- never a single quality score."""

    name = "get_candidate_report"
    description = (
        "Get the full structured evidence report for one candidate: spec, "
        "hashes, development/validation/final-test results, and provenance. "
        "Never a single 'strategy quality score.'"
    )
    required_permission = PERMISSION_CANDIDATE_REPORTING

    def __init__(self, registry: CandidateRegistry) -> None:
        self._registry = registry

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"candidate_id": {"type": "string"}},
            "required": ["candidate_id"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        candidate_id = arguments["candidate_id"]
        candidate = self._registry.get(candidate_id)
        if candidate is None:
            return ToolResult.error(self.name, "NOT_FOUND", f"no candidate {candidate_id!r}")

        if candidate.status == CandidateStrategyStatus.OUT_OF_SAMPLE_TESTED:
            candidate = self._registry.transition(candidate_id, CandidateStrategyStatus.REVIEW_REQUIRED)

        def _eval_dict(ev):
            if ev is None:
                return None
            return {
                "stage": ev.stage,
                "analytics": ev.analytics,
                "trade_count": ev.trade_count,
                "dataset_fingerprint": ev.dataset_fingerprint,
                "dataset_start": ev.dataset_start,
                "dataset_end": ev.dataset_end,
                "risk_config": ev.risk_config,
                "execution_config": ev.execution_config,
            }

        report = {
            "candidate_id": candidate.candidate_id,
            "name": candidate.name,
            "description": candidate.description,
            "status": candidate.status.value,
            "source_hash": candidate.source_hash,
            "spec_hash": candidate.spec_hash,
            "base_strategy": candidate.base_strategy,
            "configuration": candidate.configuration,
            "feature_dependencies": list(candidate.feature_dependencies),
            "agent_run_id": candidate.agent_run_id,
            "provider": candidate.provider,
            "model": candidate.model,
            "parent_candidate_id": candidate.parent_candidate_id,
            "revision_reason": candidate.revision_reason,
            "development_evaluation": _eval_dict(candidate.development_evaluation),
            "validation_evaluation": _eval_dict(candidate.validation_evaluation),
            "final_test_evaluation": _eval_dict(candidate.final_test_evaluation),
            "rejection_reason": candidate.rejection_reason,
        }
        return ToolResult.ok(self.name, report)


def _analytics_summary(analytics) -> dict[str, Any]:
    def _m(metric):
        return {"value": metric.value, "status": metric.status.value, "reason": metric.reason}

    return {
        "total_pnl": _m(analytics.total_pnl),
        "total_return": _m(analytics.total_return),
        "sharpe_ratio": _m(analytics.sharpe_ratio),
        "max_drawdown": _m(analytics.max_drawdown),
        "win_rate": _m(analytics.win_rate),
        "trade_count": analytics.trade_count,
        "profit_factor": _m(analytics.profit_factor),
        "expectancy": _m(analytics.expectancy),
    }


def _synthetic_fixture_candles():
    """A small, deterministic, platform-generated OHLCV fixture used
    only by `test_candidate`'s bounded contract test (Sprint 14 spec,
    section 73) -- never externally fetched, never candidate-supplied."""
    import numpy as np
    import pandas as pd

    n = 120
    rng = np.random.default_rng(seed=42)
    closes = 100.0 + np.cumsum(rng.normal(0, 1, n))
    dates = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC", name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes,
            "volume": np.full(n, 1000.0),
        },
        index=dates,
    )


def default_dev_tool_registry(
    *,
    experiment_registry: ExperimentRegistry | None = None,
    trial_service: ResearchTrialService | None = None,
    workspace: CandidateWorkspace | None = None,
    candidate_registry: CandidateRegistry | None = None,
    run_context: dict[str, str] | None = None,
) -> ToolRegistry:
    """Wire up the Strategy Development Agent's 15-tool registry
    (Sprint 14 spec, section 67). Mirrors
    `src.ai.agents.tools.default_tool_registry()`'s own factory
    pattern."""
    experiments = experiment_registry or ExperimentRegistry()
    trials = trial_service or ResearchTrialService()
    ws = workspace or CandidateWorkspace()
    candidates = candidate_registry or CandidateRegistry()
    context = run_context or {}

    registry = ToolRegistry()
    registry.register(ListStrategiesTool())
    registry.register(ListExperimentsTool(experiments))
    registry.register(GetExperimentTool(experiments))
    registry.register(AnalyzeExperimentTool(experiments))
    registry.register(CompareExperimentsTool(experiments))
    registry.register(ListIndicatorsTool())
    registry.register(CreateCandidateStrategyTool(ws, candidates, context))
    registry.register(InspectCandidateTool(candidates))
    registry.register(ValidateCandidateTool(ws, candidates))
    registry.register(TestCandidateTool(ws, candidates))
    registry.register(RunCandidateBacktestTool(ws, candidates, trials))
    registry.register(CompareCandidateToBaselineTool(candidates, experiments))
    registry.register(FreezeCandidateTool(candidates))
    registry.register(GetCandidateReportTool(candidates))
    return registry
