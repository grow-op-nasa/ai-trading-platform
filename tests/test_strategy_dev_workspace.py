"""Tests for the candidate domain models, lifecycle state machine,
workspace isolation, and the test-set lock's storage-layer enforcement
-- src/ai/agents/strategy_dev/{models,workspace}.py (Sprint 14 spec,
sections 1-5, 21-23, 34-36, 55, 85, 90-91, Phase 9).

`tests/test_strategy_dev_tools.py` and
`tests/test_strategy_dev_agent_loop.py` already exercise the test-set
lock through the tool/agent layers (`RunCandidateBacktestTool`'s own
status checks). This file is the complementary, independent proof that
the storage layer (`CandidateRegistry`) enforces the same invariants on
its own -- so a future tool bug (or a bypass of `RunCandidateBacktestTool`
entirely) still cannot violate the test-set lock or candidate
immutability. No network, no LLM, no joblib/sklearn dependency --
`workspace.py`/`models.py` do not import `ResearchTrialService`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.ai.agents.strategy_dev.models import (
    CandidateEvaluation,
    CandidateStrategy,
    CandidateStrategySpec,
    CandidateStrategyStatus,
    IMMUTABLE_AFTER,
    TERMINAL_STATES,
    utc_now,
)
from src.ai.agents.strategy_dev.workspace import (
    CandidateImmutableError,
    CandidateRegistry,
    CandidateWorkspace,
    InvalidTransitionError,
    sha256_text,
)

S = CandidateStrategyStatus


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


def _evaluation(stage: str, trade_count: int = 3) -> CandidateEvaluation:
    return CandidateEvaluation(
        trial_id="trial-1",
        stage=stage,
        dataset_fingerprint="fp-1",
        dataset_start="2024-01-01",
        dataset_end="2024-03-01",
        risk_config={"max_position_pct": 0.1},
        execution_config={"timing": "SIGNAL_BAR_CLOSE"},
        analytics={},
        trade_count=trade_count,
    )


@pytest.fixture
def workspace(tmp_path) -> CandidateWorkspace:
    return CandidateWorkspace(base_dir=tmp_path / "candidates")


@pytest.fixture
def registry(tmp_path) -> CandidateRegistry:
    return CandidateRegistry(base_dir=tmp_path / "candidates")


def _make(registry: CandidateRegistry, **overrides) -> CandidateStrategy:
    spec = _spec()
    kwargs = dict(
        candidate_id="candidate-001",
        spec=spec,
        source="class X: pass",
        strategy_interface_version="v1",
        agent_run_id="run-abc",
        provider="anthropic",
        model="claude-x",
        system_prompt_version="v1",
        tool_schema_version="hash1",
        policy_hash="hash2",
    )
    kwargs.update(overrides)
    return registry.create(**kwargs)


# ---------------------------------------------------------------------------
# CandidateStrategySpec
# ---------------------------------------------------------------------------


def test_spec_rejects_empty_required_fields():
    with pytest.raises(ValueError):
        _spec(research_question="")
    with pytest.raises(ValueError):
        _spec(strategy_name="")


def test_spec_describe_is_json_safe_and_deterministic():
    spec = _spec()
    first = spec.describe()
    second = spec.describe()
    assert first == second
    assert first["indicator_dependencies"] == ["EMA"]  # tuple -> list for JSON safety


# ---------------------------------------------------------------------------
# CandidateStrategy identity / immutability flags
# ---------------------------------------------------------------------------


def test_candidate_requires_timezone_aware_created_at():
    spec = _spec()
    with pytest.raises(ValueError):
        CandidateStrategy(
            candidate_id="c1",
            name=spec.strategy_name,
            description=spec.strategy_description,
            source_hash="h1",
            spec_hash="h2",
            strategy_interface_version="v1",
            created_at=datetime.now(),  # naive -- must be rejected
            created_by="agent",
            status=S.DRAFT,
            base_strategy=None,
            configuration={},
            feature_dependencies=(),
            agent_run_id="run-1",
            provider="anthropic",
            model="claude-x",
            system_prompt_version="v1",
            tool_schema_version="hash1",
            policy_hash="hash2",
        )


def test_is_immutable_and_is_terminal_match_the_spec():
    # A plain loop rather than @pytest.mark.parametrize: this repo's
    # sandbox test runner resolves fixtures by inspecting the wrapped
    # function's signature (via functools.wraps' __wrapped__), which
    # collides with parametrize argument names when a test also takes a
    # real fixture -- real pytest has no such issue, but a plain loop
    # here is simpler and sidesteps it either way.
    cases = [
        (S.DRAFT, False, False),
        (S.VALIDATING, False, False),
        (S.VALIDATED, False, False),
        (S.DEVELOPMENT_TESTED, False, False),
        (S.FROZEN, True, False),
        (S.OUT_OF_SAMPLE_TESTED, True, False),
        (S.REVIEW_REQUIRED, True, False),
        (S.APPROVED, True, False),
        (S.REJECTED, True, True),
        (S.PROMOTED, True, True),
    ]
    spec = _spec()
    for status, expected_immutable, expected_terminal in cases:
        candidate = CandidateStrategy(
            candidate_id="c1",
            name=spec.strategy_name,
            description=spec.strategy_description,
            source_hash="h1",
            spec_hash="h2",
            strategy_interface_version="v1",
            created_at=utc_now(),
            created_by="agent",
            status=status,
            base_strategy=None,
            configuration={},
            feature_dependencies=(),
            agent_run_id="run-1",
            provider="anthropic",
            model="claude-x",
            system_prompt_version="v1",
            tool_schema_version="hash1",
            policy_hash="hash2",
        )
        assert candidate.is_immutable() is expected_immutable, status
        assert candidate.is_terminal() is expected_terminal, status


def test_immutable_after_and_terminal_states_are_disjoint_from_early_lifecycle():
    early = {S.DRAFT, S.VALIDATING, S.VALIDATED, S.DEVELOPMENT_TESTED}
    assert early.isdisjoint(IMMUTABLE_AFTER)
    assert early.isdisjoint(TERMINAL_STATES)
    assert TERMINAL_STATES <= IMMUTABLE_AFTER


# ---------------------------------------------------------------------------
# sha256_text -- the identity primitive
# ---------------------------------------------------------------------------


def test_sha256_text_is_deterministic_and_sensitive_to_single_char():
    a = sha256_text("class X: pass")
    b = sha256_text("class X: pass")
    c = sha256_text("class X:  pass")  # one extra space
    assert a == b
    assert a != c
    assert len(a) == 64  # hex digest


# ---------------------------------------------------------------------------
# CandidateRegistry.create / get / list_ids
# ---------------------------------------------------------------------------


def test_create_computes_source_and_spec_hashes(registry):
    candidate = _make(registry)
    assert candidate.source_hash == sha256_text("class X: pass")
    assert candidate.status == S.DRAFT
    assert candidate.created_at.tzinfo is not None
    assert candidate.created_by == "agent"


def test_get_round_trips_through_json(registry):
    _make(registry)
    reloaded = registry.get("candidate-001")
    assert reloaded is not None
    assert reloaded.status == S.DRAFT
    assert reloaded.feature_dependencies == ("EMA",)


def test_get_missing_candidate_returns_none(registry):
    assert registry.get("nope") is None


def test_list_ids_sorted_and_reflects_all_created(registry):
    _make(registry, candidate_id="candidate-b")
    _make(registry, candidate_id="candidate-a")
    assert registry.list_ids() == ["candidate-a", "candidate-b"]


def test_list_ids_empty_when_base_dir_missing(tmp_path):
    registry = CandidateRegistry(base_dir=tmp_path / "does-not-exist")
    assert registry.list_ids() == []


# ---------------------------------------------------------------------------
# Lifecycle transition graph -- exhaustive valid/invalid edges
# ---------------------------------------------------------------------------

_VALID_EDGES = {
    (S.DRAFT, S.VALIDATING),
    (S.DRAFT, S.REJECTED),
    (S.VALIDATING, S.VALIDATED),
    (S.VALIDATING, S.REJECTED),
    (S.VALIDATED, S.DEVELOPMENT_TESTED),
    (S.VALIDATED, S.REJECTED),
    (S.DEVELOPMENT_TESTED, S.FROZEN),
    (S.DEVELOPMENT_TESTED, S.REJECTED),
    (S.FROZEN, S.OUT_OF_SAMPLE_TESTED),
    (S.OUT_OF_SAMPLE_TESTED, S.REVIEW_REQUIRED),
    (S.REVIEW_REQUIRED, S.APPROVED),
    (S.REVIEW_REQUIRED, S.REJECTED),
    (S.APPROVED, S.PROMOTED),
    (S.APPROVED, S.REJECTED),
}

# APPROVED/PROMOTED are guarded (ADR-0049): reachable only via approve()/promote(),
# never the generic transition(); covered by the dedicated tests below.
_GUARDED = {S.APPROVED, S.PROMOTED}


def test_valid_transition_succeeds(registry):
    # Plain loop -- see the note in test_is_immutable_and_is_terminal_match_the_spec.
    path_to = {
        S.DRAFT: [],
        S.VALIDATING: [S.VALIDATING],
        S.VALIDATED: [S.VALIDATING, S.VALIDATED],
        S.DEVELOPMENT_TESTED: [S.VALIDATING, S.VALIDATED, S.DEVELOPMENT_TESTED],
        S.FROZEN: [S.VALIDATING, S.VALIDATED, S.DEVELOPMENT_TESTED, S.FROZEN],
        S.OUT_OF_SAMPLE_TESTED: [
            S.VALIDATING, S.VALIDATED, S.DEVELOPMENT_TESTED, S.FROZEN, S.OUT_OF_SAMPLE_TESTED,
        ],
        S.REVIEW_REQUIRED: [
            S.VALIDATING, S.VALIDATED, S.DEVELOPMENT_TESTED, S.FROZEN,
            S.OUT_OF_SAMPLE_TESTED, S.REVIEW_REQUIRED,
        ],
    }
    for index, (start, end) in enumerate(sorted(_VALID_EDGES, key=str)):
        if end in _GUARDED or start in _GUARDED:
            continue
        candidate_id = f"candidate-{index}"
        _make(registry, candidate_id=candidate_id)
        for step in path_to[start]:
            registry.transition(candidate_id, step)
        candidate = registry.transition(candidate_id, end)
        assert candidate.status == end, (start, end)


_ALL_STATES = list(S)
_INVALID_TARGETS_FROM_DRAFT = sorted(
    (state for state in _ALL_STATES if state != S.DRAFT and (S.DRAFT, state) not in _VALID_EDGES),
    key=str,
)


def test_every_illegal_target_from_draft_is_rejected(registry):
    # A fresh candidate is DRAFT; only VALIDATING and REJECTED are
    # legal next states (test_valid_transition_succeeds covers those).
    # Every other status -- including skipping straight to FROZEN or
    # PROMOTED -- must be refused outright.
    assert _INVALID_TARGETS_FROM_DRAFT  # sanity: the fixture list isn't empty
    for target in _INVALID_TARGETS_FROM_DRAFT:
        _make(registry, candidate_id=f"candidate-{target.value}")
        with pytest.raises(InvalidTransitionError):
            registry.transition(f"candidate-{target.value}", target)


def test_frozen_to_out_of_sample_tested_is_one_way(registry):
    _make(registry)
    for step in (S.VALIDATING, S.VALIDATED, S.DEVELOPMENT_TESTED, S.FROZEN, S.OUT_OF_SAMPLE_TESTED):
        registry.transition("candidate-001", step)
    # No transition back to FROZEN, VALIDATED, DRAFT, etc.
    for illegal in (S.FROZEN, S.VALIDATED, S.DRAFT, S.DEVELOPMENT_TESTED):
        with pytest.raises(InvalidTransitionError):
            registry.transition("candidate-001", illegal)


def test_terminal_states_accept_no_further_transition(registry):
    _make(registry)
    registry.reject("candidate-001", "did not pass validation")
    assert registry.get("candidate-001").status == S.REJECTED
    for target in S:
        with pytest.raises(InvalidTransitionError):
            registry.transition("candidate-001", target)


def test_transition_on_missing_candidate_raises_keyerror(registry):
    with pytest.raises(KeyError):
        registry.transition("nope", S.VALIDATING)


# ---------------------------------------------------------------------------
# The test-set lock -- storage-layer enforcement (CandidateRegistry itself,
# independent of RunCandidateBacktestTool's own pre-checks).
# ---------------------------------------------------------------------------


def test_final_test_evaluation_refused_before_frozen(registry):
    _make(registry)
    registry.transition("candidate-001", S.VALIDATING)
    registry.transition("candidate-001", S.VALIDATED)
    registry.transition("candidate-001", S.DEVELOPMENT_TESTED)  # not yet FROZEN
    with pytest.raises(CandidateImmutableError):
        registry.record_evaluation("candidate-001", "final_test", _evaluation("final_test"))
    assert registry.get("candidate-001").final_test_evaluation is None


def test_final_test_evaluation_allowed_exactly_once_after_frozen(registry):
    _make(registry)
    for step in (S.VALIDATING, S.VALIDATED, S.DEVELOPMENT_TESTED, S.FROZEN):
        registry.transition("candidate-001", step)
    registry.record_evaluation("candidate-001", "final_test", _evaluation("final_test"))
    assert registry.get("candidate-001").final_test_evaluation is not None

    # A second attempt -- even while still nominally FROZEN, or after
    # transitioning on to OUT_OF_SAMPLE_TESTED -- must be refused.
    with pytest.raises(CandidateImmutableError):
        registry.record_evaluation("candidate-001", "final_test", _evaluation("final_test", trade_count=99))

    registry.transition("candidate-001", S.OUT_OF_SAMPLE_TESTED)
    with pytest.raises(CandidateImmutableError):
        registry.record_evaluation("candidate-001", "final_test", _evaluation("final_test", trade_count=99))

    # The original evaluation must be untouched by the refused attempts.
    assert registry.get("candidate-001").final_test_evaluation.trade_count == 3


def test_development_and_validation_evaluations_refused_once_immutable(registry):
    _make(registry)
    for step in (S.VALIDATING, S.VALIDATED, S.DEVELOPMENT_TESTED, S.FROZEN):
        registry.transition("candidate-001", step)
    with pytest.raises(CandidateImmutableError):
        registry.record_evaluation("candidate-001", "development", _evaluation("development"))
    with pytest.raises(CandidateImmutableError):
        registry.record_evaluation("candidate-001", "validation", _evaluation("validation"))


def test_development_and_validation_evaluations_allowed_before_freeze(registry):
    _make(registry)
    registry.transition("candidate-001", S.VALIDATING)
    registry.transition("candidate-001", S.VALIDATED)
    candidate = registry.record_evaluation("candidate-001", "development", _evaluation("development"))
    assert candidate.development_evaluation is not None
    candidate = registry.record_evaluation("candidate-001", "validation", _evaluation("validation"))
    assert candidate.validation_evaluation is not None
    # Freezing afterward must not retroactively invalidate development/
    # validation evidence already on record.
    registry.transition("candidate-001", S.DEVELOPMENT_TESTED)
    frozen = registry.transition("candidate-001", S.FROZEN)
    assert frozen.development_evaluation is not None
    assert frozen.validation_evaluation is not None


def test_record_evaluation_rejects_unknown_stage(registry):
    _make(registry)
    with pytest.raises(ValueError):
        registry.record_evaluation("candidate-001", "not_a_real_stage", _evaluation("development"))


def test_record_evaluation_on_missing_candidate_raises_keyerror(registry):
    with pytest.raises(KeyError):
        registry.record_evaluation("nope", "development", _evaluation("development"))


# ---------------------------------------------------------------------------
# Promotion -- human-only, requires full evidence (CandidateRegistry.promote
# is never called by any agent tool; see src/cli/strategy_promote.py).
# ---------------------------------------------------------------------------


def _to_review_required(registry, candidate_id="candidate-001", with_final=True):
    _make(registry, candidate_id=candidate_id)
    for step in (S.VALIDATING, S.VALIDATED, S.DEVELOPMENT_TESTED, S.FROZEN):
        registry.transition(candidate_id, step)
    if with_final:
        registry.record_evaluation(candidate_id, "final_test", _evaluation("final_test"))
    for step in (S.OUT_OF_SAMPLE_TESTED, S.REVIEW_REQUIRED):
        registry.transition(candidate_id, step)
    return registry.get(candidate_id)


_REGISTRATION = {
    "strategy_name": "ema_vol_filter",
    "module": "src/strategies/promoted/ema_vol_filter.py",
    "class_name": "EmaVolFilter",
    "module_sha256": "a" * 64,
    "registered_at": "2026-10-11T00:00:00+00:00",
}


def test_approve_requires_review_required_and_final_test_evidence(registry):
    _make(registry)
    cand = registry.get("candidate-001")
    with pytest.raises(InvalidTransitionError):
        registry.approve("candidate-001", approved_by="t", source_hash=cand.source_hash, spec_hash=cand.spec_hash)

    cand = _to_review_required(registry, "candidate-002", with_final=False)
    # REVIEW_REQUIRED reached via transition without a final evaluation.
    with pytest.raises(InvalidTransitionError):
        registry.approve("candidate-002", approved_by="t", source_hash=cand.source_hash, spec_hash=cand.spec_hash)


def test_approved_is_not_promoted_and_records_the_approval(registry):
    cand = _to_review_required(registry)
    approved = registry.approve("candidate-001", approved_by="human", source_hash=cand.source_hash, spec_hash=cand.spec_hash)
    assert approved.status == S.APPROVED
    assert approved.approval["approved_by"] == "human"
    assert approved.registration is None
    assert not approved.is_production_registered()


def test_approve_rejects_changed_hashes(registry):
    cand = _to_review_required(registry)
    with pytest.raises(InvalidTransitionError):
        registry.approve("candidate-001", approved_by="t", source_hash="x" * 64, spec_hash=cand.spec_hash)


def test_promote_requires_approval_and_a_registration_record(registry):
    cand = _to_review_required(registry)
    # REVIEW_REQUIRED (not yet APPROVED) cannot be promoted.
    with pytest.raises(InvalidTransitionError):
        registry.promote("candidate-001", dict(_REGISTRATION))
    registry.approve("candidate-001", approved_by="t", source_hash=cand.source_hash, spec_hash=cand.spec_hash)
    # APPROVED but no registration -> still refused (human approval alone is not PROMOTED).
    with pytest.raises(InvalidTransitionError):
        registry.promote("candidate-001")
    with pytest.raises(InvalidTransitionError):
        registry.promote("candidate-001", {"strategy_name": "x"})
    assert registry.get("candidate-001").status == S.APPROVED


def test_promote_succeeds_with_registration_record(registry):
    cand = _to_review_required(registry)
    registry.approve("candidate-001", approved_by="t", source_hash=cand.source_hash, spec_hash=cand.spec_hash)
    promoted = registry.promote("candidate-001", dict(_REGISTRATION))
    assert promoted.status == S.PROMOTED
    assert promoted.is_production_registered()


def test_generic_transition_cannot_reach_approved_or_promoted(registry):
    _to_review_required(registry)
    for target in (S.APPROVED, S.PROMOTED):
        with pytest.raises(InvalidTransitionError):
            registry.transition("candidate-001", target)


def test_registered_candidate_cannot_be_rejected(registry):
    cand = _to_review_required(registry)
    registry.approve("candidate-001", approved_by="t", source_hash=cand.source_hash, spec_hash=cand.spec_hash)
    registry.promote("candidate-001", dict(_REGISTRATION))
    with pytest.raises(InvalidTransitionError):
        registry.reject("candidate-001", "too late")


def test_promote_on_missing_candidate_raises_keyerror(registry):
    with pytest.raises(KeyError):
        registry.promote("nope")


def test_legacy_promoted_record_is_flagged_not_reinterpreted(registry):
    from src.ai.agents.strategy_dev.models import registration_state
    import json

    _to_review_required(registry)
    path = registry._record_path("candidate-001")
    raw = json.loads(path.read_text())
    raw["status"] = "PROMOTED"  # a pre-ADR-0049 record: promoted, no registration
    raw.pop("approval", None)
    raw.pop("registration", None)
    path.write_text(json.dumps(raw))
    legacy = registry.get("candidate-001")
    assert legacy.status == S.PROMOTED
    assert not legacy.is_production_registered()
    assert registration_state(legacy) == "LEGACY_PROMOTED_UNVERIFIED"


# ---------------------------------------------------------------------------
# CandidateWorkspace -- source immutability and filesystem isolation
# ---------------------------------------------------------------------------


def test_write_source_refuses_to_overwrite_existing_candidate(workspace):
    workspace.write_source("candidate-001", "class X: pass")
    with pytest.raises(CandidateImmutableError):
        workspace.write_source("candidate-001", "class X:\n    pass\n# an edit attempt")
    # The original content must be untouched by the refused overwrite.
    assert workspace.read_source("candidate-001") == "class X: pass"


def test_each_candidate_gets_an_isolated_directory(workspace):
    workspace.write_source("candidate-a", "class A: pass")
    workspace.write_source("candidate-b", "class B: pass")
    assert workspace.read_source("candidate-a") == "class A: pass"
    assert workspace.read_source("candidate-b") == "class B: pass"
    assert workspace.candidate_dir("candidate-a") != workspace.candidate_dir("candidate-b")


def test_source_path_points_inside_the_candidate_workspace_base_dir(workspace):
    workspace.write_source("candidate-001", "class X: pass")
    path = workspace.source_path("candidate-001")
    assert workspace.base_dir in path.parents
    assert path.name == "strategy.py"


def test_candidate_workspace_and_registry_share_one_directory_per_candidate(tmp_path):
    # Sprint 14 spec, section 90: workspace source and registry manifest
    # live side by side under the same <base_dir>/<candidate_id>/, not
    # in two separate storage systems that could drift out of sync.
    base = tmp_path / "candidates"
    workspace = CandidateWorkspace(base_dir=base)
    registry = CandidateRegistry(base_dir=base)
    _make(registry)
    workspace.write_source("candidate-001", "class X: pass")
    candidate_dir = base / "candidate-001"
    assert (candidate_dir / "strategy.py").exists()
    assert (candidate_dir / "manifest.json").exists()
