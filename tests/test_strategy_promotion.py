"""Tests for the corrected Sprint 14 promotion semantics (ADR-0049).

`PROMOTED` must mean *production-registered*: human approval alone is
`APPROVED`; only after `PromotionService` has materialized the
candidate, registered it with the strategy registry and verified it
resolves, instantiates and emits canonical `Signal`s does the candidate
become `PROMOTED`. Network-free and deterministic: every test uses temp
directories and an isolated in-memory strategy registry, so the real
production manifest and `StrategyRegistry` are never touched.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.ai.agents.strategy_dev.models import (
    CandidateEvaluation,
    CandidateStrategySpec,
    CandidateStrategyStatus,
    registration_state,
)
from src.ai.agents.strategy_dev.runner import RunnerResult
from src.ai.agents.strategy_dev.workspace import CandidateRegistry, CandidateWorkspace
from src.promotion import PromotionError, PromotionService
from src.signals.models import Signal, SignalDirection
from src.strategies.promoted import load_promoted_strategies, read_manifest
from src.strategies.base import Strategy

S = CandidateStrategyStatus
REPO_ROOT = Path(__file__).resolve().parent.parent

GOOD_SOURCE = '''\
from src.strategies.sdk import BaseStrategy
from src.strategies.history import combine, ema_component
from src.signals.models import SignalDirection


class PromoDemo(BaseStrategy):
    """Tiny EMA crossover used only by the promotion tests."""

    def __init__(self, symbol: str, fast: int = 5, slow: int = 12):
        super().__init__(name="promo_demo", symbol=symbol)
        self._fast = fast
        self._slow = slow

    def history_requirement(self):
        return combine(ema_component(self._fast), ema_component(self._slow))

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
            up = row["ema_fast"] > row["ema_slow"]
            if up and not in_position:
                signals.append(self.emit_signal(timestamp, SignalDirection.LONG, confidence=0.6))
                in_position = True
            elif not up and in_position:
                signals.append(self.emit_signal(timestamp, SignalDirection.FLAT, confidence=0.6))
                in_position = False
        return signals
'''

BAD_SOURCE = GOOD_SOURCE.replace(
    "from src.signals.models import SignalDirection",
    "import os\nfrom src.signals.models import SignalDirection",
)


def _ok_runner(source_path, candles, symbol, params, timeout=60):
    """Fast stand-in for the isolated runner: emits one canonical signal."""
    sig = Signal(timestamp=candles.index[10], symbol=symbol, direction=SignalDirection.LONG, confidence=0.5)
    from src.strategies.history import combine, ema_component
    return RunnerResult(signals=(sig,), history={"declared": True, "requirement": combine(ema_component(5), ema_component(12)).to_dict()})


def _undeclared_runner(source_path, candles, symbol, params, timeout=60):
    sig = Signal(timestamp=candles.index[10], symbol=symbol, direction=SignalDirection.LONG, confidence=0.5)
    return RunnerResult(signals=(sig,), history={"declared": False, "error": "the candidate declares no history_requirement()"})


def _invalid_history_runner(source_path, candles, symbol, params, timeout=60):
    sig = Signal(timestamp=candles.index[10], symbol=symbol, direction=SignalDirection.LONG, confidence=0.5)
    return RunnerResult(signals=(sig,), history={"declared": True, "requirement": {"min_bars": 0, "components": []}})


def _bad_runner(source_path, candles, symbol, params, timeout=60):
    return RunnerResult(error="boom")


class IsolatedRegistry:
    """An in-memory stand-in for the production StrategyRegistry."""

    def __init__(self, preexisting=("ema_cross",)):
        self.items: dict[str, type] = {n: object for n in preexisting}

    def register(self, name):
        def deco(cls):
            if name in self.items:
                raise ValueError(f"already registered: {name}")
            self.items[name] = cls
            return cls

        return deco

    def unregister(self, name):
        del self.items[name]

    def names(self):
        return sorted(self.items)

    def resolve(self, name):
        return self.items[name]


def _evaluation(stage):
    return CandidateEvaluation(
        trial_id=f"t-{stage}",
        stage=stage,
        dataset_fingerprint="fp",
        dataset_start="2024-01-01",
        dataset_end="2024-06-01",
        risk_config={"allocation_per_trade_pct": 0.1},
        execution_config={"timing": "SIGNAL_BAR_CLOSE"},
        analytics={},
        trade_count=5,
    )


def _make_candidate(candidates, workspace, source=GOOD_SOURCE, *, final=True, cid="cand-1", name="promo_demo"):
    spec = CandidateStrategySpec(
        research_question="q",
        hypothesis="h",
        strategy_name=name,
        strategy_description="d",
        entry_logic="e",
        exit_logic="x",
        indicator_dependencies=("EMA",),
        parameters={"fast": 5, "slow": 12},
        symbol="QQQ",
        interval="1d",
        signal_semantics="LONG/FLAT",
        expected_behavior="b",
    )
    candidates.create(
        candidate_id=cid,
        spec=spec,
        source=source,
        strategy_interface_version="v1",
        agent_run_id="run-1",
        provider="fake",
        model="fake-1",
        system_prompt_version="v2",
        tool_schema_version="t1",
        policy_hash="p" * 8,
    )
    workspace.write_source(cid, source)
    for step in (S.VALIDATING, S.VALIDATED):
        candidates.transition(cid, step)
    candidates.record_evaluation(cid, "development", _evaluation("development"))
    candidates.record_evaluation(cid, "validation", _evaluation("validation"))
    candidates.transition(cid, S.DEVELOPMENT_TESTED)
    candidates.transition(cid, S.FROZEN)
    if final:
        candidates.record_evaluation(cid, "final_test", _evaluation("final_test"))
    candidates.transition(cid, S.OUT_OF_SAMPLE_TESTED)
    candidates.transition(cid, S.REVIEW_REQUIRED)
    return candidates.get(cid)


@pytest.fixture
def env(tmp_path):
    candidates = CandidateRegistry(tmp_path / "cands")
    workspace = CandidateWorkspace(tmp_path / "cands")
    promoted_dir = tmp_path / "promoted"
    promoted_dir.mkdir()
    (promoted_dir / "manifest.json").write_text('{"entries": [], "schema_version": 1}')
    reg = IsolatedRegistry()

    def service(runner=_ok_runner, **kw):
        return PromotionService(
            candidates,
            workspace,
            promoted_dir=promoted_dir,
            register=kw.get("register", reg.register),
            unregister=reg.unregister,
            registered_names=reg.names,
            resolve=reg.resolve,
            runner=runner,
        )

    class E:
        pass

    e = E()
    e.candidates, e.workspace, e.dir, e.reg, e.service = candidates, workspace, promoted_dir, reg, service
    yield e
    for name in list(reg.items):
        if name != "ema_cross":
            sys.modules.pop(f"src.strategies.promoted.{name}", None)


# ---- 1-4: a valid candidate is approved, registered, resolvable ------------


def test_valid_candidate_is_approved_registered_and_only_then_promoted(env):
    _make_candidate(env.candidates, env.workspace)
    result = env.service().promote("cand-1", human_approved=True)

    cand = env.candidates.get("cand-1")
    assert cand.status == S.PROMOTED
    assert cand.is_production_registered()
    assert registration_state(cand) == "REGISTERED"
    assert cand.approval["approved_by"] == "human-cli"
    assert cand.registration["strategy_name"] == "promo_demo"
    assert result.strategy_name == "promo_demo"
    assert (env.dir / "promo_demo.py").is_file()


def test_registry_resolves_and_instantiates_the_promoted_strategy(env):
    _make_candidate(env.candidates, env.workspace)
    env.service().promote("cand-1", human_approved=True)

    cls = env.reg.resolve("promo_demo")
    instance = cls(symbol="QQQ")
    assert isinstance(instance, Strategy)  # the normal Strategy contract
    assert instance.name == "promo_demo"


def test_registered_strategy_emits_canonical_signals(env):
    from src.promotion import contract_candles

    _make_candidate(env.candidates, env.workspace)
    env.service().promote("cand-1", human_approved=True)
    instance = env.reg.resolve("promo_demo")(symbol="QQQ")
    signals = instance.generate_signals(instance.prepare(contract_candles()))
    assert signals
    assert all(isinstance(s, Signal) and s.symbol == "QQQ" for s in signals)


def test_hashes_and_lineage_match_the_registered_entry(env):
    cand = _make_candidate(env.candidates, env.workspace)
    env.service().promote("cand-1", human_approved=True)

    (entry,) = read_manifest(env.dir)
    assert entry.candidate_id == "cand-1"
    assert entry.source_hash == cand.source_hash
    assert entry.spec_hash == cand.spec_hash
    assert entry.agent_run_id == "run-1"
    # The module on disk is the candidate source *verbatim*.
    assert (env.dir / "promo_demo.py").read_text() == GOOD_SOURCE
    assert env.candidates.get("cand-1").registration["module_sha256"] == cand.source_hash


def test_real_isolated_runner_contract_test_passes_for_a_valid_candidate(env):
    # Uses the real subprocess runner (not the fake) once, end to end.
    probe = subprocess.run(
        [sys.executable, "-c", "import loguru"],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
    )
    if probe.returncode != 0:  # the isolated subprocess imports src.strategies.sdk -> loguru
        pytest.skip("loguru not importable in an isolated subprocess (sandbox-only)")
    from src.ai.agents.strategy_dev.runner import run_candidate

    _make_candidate(env.candidates, env.workspace)
    env.service(runner=run_candidate).promote("cand-1", human_approved=True)
    assert env.candidates.get("cand-1").status == S.PROMOTED


# ---- 5-7: evidence, validation, integrity are enforced ----------------------


def test_candidate_without_a_final_test_is_rejected(env):
    # Reach REVIEW_REQUIRED without a recorded final evaluation.
    _make_candidate(env.candidates, env.workspace, final=False)
    with pytest.raises(PromotionError, match="final_out_of_sample_evaluation"):
        env.service().promote("cand-1", human_approved=True)
    assert env.candidates.get("cand-1").status == S.REVIEW_REQUIRED
    assert read_manifest(env.dir) == []


def test_candidate_failing_static_validation_is_rejected(env):
    _make_candidate(env.candidates, env.workspace, source=BAD_SOURCE)
    with pytest.raises(PromotionError, match="static_safety"):
        env.service().promote("cand-1", human_approved=True)
    assert env.candidates.get("cand-1").status == S.REVIEW_REQUIRED
    assert not (env.dir / "promo_demo.py").exists()


def test_altered_source_hash_is_rejected(env):
    _make_candidate(env.candidates, env.workspace)
    env.workspace.source_path("cand-1").write_text(GOOD_SOURCE + "\n# tampered\n")
    with pytest.raises(PromotionError, match="source_hash_matches"):
        env.service().promote("cand-1", human_approved=True)
    assert env.candidates.get("cand-1").status == S.REVIEW_REQUIRED


def test_failed_contract_test_is_rejected(env):
    _make_candidate(env.candidates, env.workspace)
    with pytest.raises(PromotionError, match="strategy_signal_contract"):
        env.service(runner=_bad_runner).promote("cand-1", human_approved=True)
    assert env.candidates.get("cand-1").status == S.REVIEW_REQUIRED


def test_a_candidate_without_a_history_requirement_is_not_promotable(env):
    _make_candidate(env.candidates, env.workspace)
    with pytest.raises(PromotionError, match="history_requirement_declared"):
        env.service(runner=_undeclared_runner).promote("cand-1", human_approved=True)
    assert env.candidates.get("cand-1").status == S.REVIEW_REQUIRED
    assert not read_manifest(env.dir)


def test_a_candidate_with_an_invalid_history_requirement_is_not_promotable(env):
    _make_candidate(env.candidates, env.workspace)
    with pytest.raises(PromotionError, match="history_requirement_declared"):
        env.service(runner=_invalid_history_runner).promote("cand-1", human_approved=True)
    assert not read_manifest(env.dir)


def test_the_preflight_report_shows_the_declared_requirement(env):
    _make_candidate(env.candidates, env.workspace)
    report = env.service().preflight("cand-1")
    names = {n: (ok, d) for n, ok, d in report.checks}
    assert names["history_requirement_declared"][0] and "complete bars required" in names["history_requirement_declared"][1]


def test_explicit_human_approval_is_required(env):
    _make_candidate(env.candidates, env.workspace)
    with pytest.raises(PromotionError, match="human approval"):
        env.service().promote("cand-1", human_approved=False)
    assert env.candidates.get("cand-1").status == S.REVIEW_REQUIRED


def test_incomplete_provenance_is_rejected(env):
    import json as _json

    _make_candidate(env.candidates, env.workspace)
    path = env.candidates._record_path("cand-1")
    raw = _json.loads(path.read_text())
    raw["final_test_evaluation"]["execution_config"] = {}
    path.write_text(_json.dumps(raw))
    with pytest.raises(PromotionError, match="provenance_complete"):
        env.service().promote("cand-1", human_approved=True)


# ---- 8-9: duplicates / overwrite ---------------------------------------------


def test_repeat_promotion_is_idempotent_and_does_not_duplicate(env):
    _make_candidate(env.candidates, env.workspace)
    first = env.service().promote("cand-1", human_approved=True)
    second = env.service().promote("cand-1", human_approved=True)
    assert second.already_promoted
    assert len(read_manifest(env.dir)) == 1
    assert env.reg.names().count("promo_demo") == 1
    assert first.entry == second.entry


def test_existing_production_strategy_cannot_be_overwritten(env):
    _make_candidate(env.candidates, env.workspace, name="ema_cross")
    with pytest.raises(PromotionError, match="no_name_collision"):
        env.service().promote("cand-1", human_approved=True)
    assert env.reg.items["ema_cross"] is object  # untouched
    assert env.candidates.get("cand-1").status == S.REVIEW_REQUIRED


def test_second_candidate_cannot_reuse_a_promoted_name(env):
    _make_candidate(env.candidates, env.workspace)
    env.service().promote("cand-1", human_approved=True)
    _make_candidate(env.candidates, env.workspace, cid="cand-2")
    with pytest.raises(PromotionError, match="no_name_collision"):
        env.service().promote("cand-2", human_approved=True)
    assert len(read_manifest(env.dir)) == 1


def test_existing_production_file_is_never_overwritten(env):
    _make_candidate(env.candidates, env.workspace)
    (env.dir / "promo_demo.py").write_text("# someone else's file\n")
    with pytest.raises(PromotionError, match="overwrite"):
        env.service().promote("cand-1", human_approved=True)
    assert (env.dir / "promo_demo.py").read_text() == "# someone else's file\n"


# ---- 10: failures never leave a false PROMOTED -------------------------------


def test_registration_failure_leaves_candidate_approved_and_rolls_back(env):
    _make_candidate(env.candidates, env.workspace)

    def exploding_register(name):
        raise RuntimeError("registry unavailable")

    with pytest.raises(PromotionError, match="rolled back"):
        env.service(register=exploding_register).promote("cand-1", human_approved=True)

    cand = env.candidates.get("cand-1")
    assert cand.status == S.APPROVED  # approved, NOT promoted
    assert registration_state(cand) == "APPROVED_NOT_REGISTERED"
    assert read_manifest(env.dir) == []
    assert not (env.dir / "promo_demo.py").exists()
    assert "promo_demo" not in env.reg.names()


def test_status_write_failure_after_registration_is_recoverable_and_idempotent(env, monkeypatch):
    _make_candidate(env.candidates, env.workspace)
    real_promote = env.candidates.promote

    def failing_promote(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(env.candidates, "promote", failing_promote)
    with pytest.raises(PromotionError, match="IS registered"):
        env.service().promote("cand-1", human_approved=True)
    assert env.candidates.get("cand-1").status == S.APPROVED
    assert len(read_manifest(env.dir)) == 1  # the recovery record

    monkeypatch.setattr(env.candidates, "promote", real_promote)
    result = env.service().promote("cand-1", human_approved=True)
    assert result.resumed
    assert env.candidates.get("cand-1").status == S.PROMOTED
    assert len(read_manifest(env.dir)) == 1
    assert env.reg.names().count("promo_demo") == 1


def test_name_mismatch_between_strategy_and_registered_id_rolls_back(env):
    _make_candidate(env.candidates, env.workspace)
    with pytest.raises(PromotionError, match="rolled back"):
        env.service().promote("cand-1", human_approved=True, strategy_name="different_id")
    assert env.candidates.get("cand-1").status == S.APPROVED
    assert read_manifest(env.dir) == []


def test_legacy_promoted_candidate_is_reported_not_reinterpreted(env):
    _make_candidate(env.candidates, env.workspace)
    path = env.candidates._record_path("cand-1")
    raw = json.loads(path.read_text())
    raw["status"] = "PROMOTED"
    path.write_text(json.dumps(raw))
    with pytest.raises(PromotionError, match="LEGACY"):
        env.service().promote("cand-1", human_approved=True)
    assert read_manifest(env.dir) == []
    assert registration_state(env.candidates.get("cand-1")) == "LEGACY_PROMOTED_UNVERIFIED"


# ---- the manifest loader fails closed ----------------------------------------


def test_loader_registers_manifest_entries_and_detects_tampering(env):
    _make_candidate(env.candidates, env.workspace)
    env.service().promote("cand-1", human_approved=True)

    fresh = IsolatedRegistry(preexisting=())
    report = load_promoted_strategies(env.dir, register=fresh.register)
    assert report.loaded == ["promo_demo"] and not report.errors
    assert fresh.resolve("promo_demo")(symbol="X").name == "promo_demo"

    (env.dir / "promo_demo.py").write_text(GOOD_SOURCE + "\n# swapped\n")
    tampered = IsolatedRegistry(preexisting=())
    report = load_promoted_strategies(env.dir, register=tampered.register)
    assert report.loaded == []
    assert "no longer matches" in report.errors["promo_demo"]
    assert tampered.names() == []


def test_loader_ignores_files_not_listed_in_the_manifest(env):
    (env.dir / "rogue.py").write_text("raise SystemExit('must never be imported')\n")
    report = load_promoted_strategies(env.dir, register=IsolatedRegistry(preexisting=()).register)
    assert report.loaded == [] and report.errors == {}


# ---- CLI ---------------------------------------------------------------------


def test_cli_promotes_with_yes_and_prints_truthful_summary(env, monkeypatch, capsys, tmp_path):
    from src.cli.strategy_promote import run_strategy_promote

    monkeypatch.chdir(tmp_path)
    cands = CandidateRegistry()  # cwd-relative default, now under tmp_path
    ws = CandidateWorkspace()
    _make_candidate(cands, ws)
    rc = run_strategy_promote(
        ["--candidate-id", "cand-1", "--yes"],
        service_factory=lambda c, w: PromotionService(
            c, w, promoted_dir=env.dir, register=env.reg.register, unregister=env.reg.unregister,
            registered_names=env.reg.names, resolve=env.reg.resolve, runner=_ok_runner,
        ),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Promoted and registered as 'promo_demo'" in out
    assert "does not deploy it" in out
    assert cands.get("cand-1").status == S.PROMOTED


def test_cli_refuses_when_preflight_fails_and_changes_nothing(env, monkeypatch, capsys, tmp_path):
    from src.cli.strategy_promote import run_strategy_promote

    monkeypatch.chdir(tmp_path)
    cands = CandidateRegistry()
    ws = CandidateWorkspace()
    _make_candidate(cands, ws, source=BAD_SOURCE)
    rc = run_strategy_promote(
        ["--candidate-id", "cand-1", "--yes"],
        service_factory=lambda c, w: PromotionService(
            c, w, promoted_dir=env.dir, register=env.reg.register, unregister=env.reg.unregister,
            registered_names=env.reg.names, resolve=env.reg.resolve, runner=_ok_runner,
        ),
    )
    assert rc == 1
    assert "[FAIL] static_safety" in capsys.readouterr().out
    assert cands.get("cand-1").status == S.REVIEW_REQUIRED


def test_cli_reports_a_missing_candidate(monkeypatch, capsys, tmp_path):
    from src.cli.strategy_promote import run_strategy_promote

    monkeypatch.chdir(tmp_path)
    assert run_strategy_promote(["--candidate-id", "nope", "--yes"]) == 2


def test_promotion_cli_imports_without_joblib_or_sklearn():
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('joblib', 'sklearn', 'anthropic'):\n"
        "            raise ImportError('blocked ' + name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "import src.cli.strategy_promote\n"
        "print('ok')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{REPO_ROOT}:/tmp/stubs"},
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "ok", out.stderr[-400:]


# ---- authority boundaries -----------------------------------------------------

_SRC = REPO_ROOT / "src"


def _agent_files():
    return [p for p in (_SRC / "ai").rglob("*.py")]


def test_no_agent_code_can_reach_promotion_or_the_promoted_package():
    forbidden = re.compile(
        r"^\s*(?:from|import)\s+src\.(promotion|cli\.strategy_promote|strategies\.promoted)\b"
        r"|_unregister_strategy|\.approve\(|\.promote\(",
        re.MULTILINE,
    )
    offenders = {
        str(p.relative_to(_SRC)): forbidden.search(p.read_text()).group(0)
        for p in _agent_files()
        if forbidden.search(p.read_text())
        and p.name not in ("workspace.py", "models.py")  # define/document approve()/promote()
    }
    assert offenders == {}


def test_promotion_service_never_touches_trading_state():
    text = "\n".join(p.read_text() for p in (_SRC / "promotion").glob("*.py"))
    for forbidden in ("src.broker", "src.execution", "src.portfolio", "src.risk", "PaperBroker", "submit_order"):
        assert forbidden not in text, forbidden


def test_agent_dev_tool_registry_has_no_promotion_or_registration_tool():
    pytest.importorskip("joblib")
    from src.ai.agents.strategy_dev.tools import default_dev_tool_registry
    from src.experiments.registry import ExperimentRegistry

    registry = default_dev_tool_registry(
        experiment_registry=ExperimentRegistry(db_path=":memory:"),
    )
    names = [t.name for t in registry.all_tools()]
    assert names
    for name in names:
        for word in ("promote", "approve", "register_strategy"):
            assert word not in name
