"""PromotionService -- src/promotion/service.py (ADR-0049).

Turns a `REVIEW_REQUIRED` candidate into a genuinely registered
production strategy, under explicit human authority:

    REVIEW_REQUIRED --(human approval)--> APPROVED
        --(materialize + register + verify)--> PROMOTED

`PROMOTED` is set **last**, and only after the production
`StrategyRegistry` can resolve, instantiate and exercise the strategy.
If registration or verification fails, the strategy is rolled back and
the candidate stays `APPROVED` (approved, not registered) -- never a
false `PROMOTED`. If the final candidate-status write fails after a
successful registration, the manifest entry is the recovery record:
re-running promotion for the same candidate is idempotent and completes
the status update without duplicating anything.

Registration is not deployment: the strategy becomes an ordinary
registered `Strategy` available for research or explicitly controlled
workflows. Nothing here touches a broker, portfolio, risk limit or
order.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from src.ai.agents.strategy_dev.models import (
    CandidateStrategy,
    CandidateStrategyStatus,
    utc_now,
)
from src.ai.agents.strategy_dev.runner import run_candidate
from src.ai.agents.strategy_dev.safety import validate_candidate_source
from src.ai.agents.strategy_dev.workspace import (
    CandidateRegistry,
    CandidateWorkspace,
    InvalidTransitionError,
    sha256_text,
)
from src.signals.models import Signal, SignalDirection
from src.strategies.base import Strategy
from src.strategies.promoted import (
    PROMOTED_DIR,
    PromotedEntry,
    load_module_class,
    read_manifest,
    write_manifest,
)
from src.strategies.registry import (
    _unregister_strategy,
    available_strategies,
    get_strategy_class,
    register_strategy,
)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
CONTRACT_SYMBOL = "CONTRACT"


class PromotionError(Exception):
    """Promotion refused or failed. The message is operator-facing and
    states exactly which state the candidate was left in."""


def contract_candles(n: int = 320) -> pd.DataFrame:
    """Deterministic, network-free OHLCV candles for the platform-owned
    Strategy/Signal contract test: a repeating trend/volatility cycle
    that gives momentum, mean-reversion and volatility-filter
    strategies alike something to react to."""
    idx = pd.date_range("2022-01-03", periods=n, freq="D", tz="UTC", name="timestamp")
    t = np.arange(n, dtype=float)
    close = 100 + 12 * np.sin(t / 9.0) + 0.05 * t + 2.5 * np.sin(t / 2.3)
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.8 + 0.4 * np.abs(np.sin(t / 5.0)),
            "low": close - 0.8 - 0.4 * np.abs(np.sin(t / 5.0)),
            "close": close,
            "volume": 1000.0 + 10.0 * (t % 17),
        },
        index=idx,
    )


@dataclass
class PreflightReport:
    candidate_id: str
    strategy_name: str | None = None
    class_name: str | None = None
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    @property
    def failures(self) -> list[tuple[str, str]]:
        return [(n, d) for n, ok, d in self.checks if not ok]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class PromotionResult:
    candidate: CandidateStrategy
    strategy_name: str
    entry: PromotedEntry
    already_promoted: bool = False
    resumed: bool = False


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _find_class_name(source: str) -> str:
    tree = ast.parse(source)
    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    if len(classes) != 1:
        raise PromotionError(f"expected exactly one top-level class in candidate source, found {classes}")
    return classes[0]


class PromotionService:
    """Args:
        candidates / workspace: the candidate stores.
        promoted_dir: where approved modules + `manifest.json` live
            (defaults to the production package; tests pass a temp dir).
        register / unregister: registry hooks (default to the
            production `StrategyRegistry`; tests may inject an
            isolated pair).
        runner: the isolated candidate runner used for the contract
            test (injectable for tests).
    """

    def __init__(
        self,
        candidates: CandidateRegistry,
        workspace: CandidateWorkspace,
        *,
        promoted_dir: Path | str = PROMOTED_DIR,
        register: Callable[[str], Callable[[type], type]] = register_strategy,
        unregister: Callable[[str], None] = _unregister_strategy,
        registered_names: Callable[[], list[str]] = available_strategies,
        resolve: Callable[[str], type] = get_strategy_class,
        runner=run_candidate,
    ) -> None:
        self._candidates = candidates
        self._workspace = workspace
        self._dir = Path(promoted_dir)
        self._register = register
        self._unregister = unregister
        self._names = registered_names
        self._resolve = resolve
        self._runner = runner

    # ------------------------------------------------------------------
    # Preflight (read-only; safe to call before asking for approval)
    # ------------------------------------------------------------------

    def preflight(self, candidate_id: str, strategy_name: str | None = None) -> PreflightReport:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise PromotionError(f"no such candidate: {candidate_id!r}")
        report = PreflightReport(candidate_id=candidate_id)

        status_ok = candidate.status in (
            CandidateStrategyStatus.REVIEW_REQUIRED,
            CandidateStrategyStatus.APPROVED,
        )
        report.add("lifecycle", status_ok, f"status is {candidate.status.value}; must be REVIEW_REQUIRED or APPROVED")
        report.add(
            "final_out_of_sample_evaluation",
            candidate.final_test_evaluation is not None,
            "a recorded final out-of-sample evaluation is required",
        )
        report.add("validation_evaluation", candidate.validation_evaluation is not None, "validation evaluation required")
        report.add("development_evaluation", candidate.development_evaluation is not None, "development evaluation required")

        prov_missing = [
            k
            for k in ("agent_run_id", "provider", "model", "system_prompt_version", "tool_schema_version", "policy_hash")
            if not getattr(candidate, k)
        ]
        evals = [candidate.development_evaluation, candidate.validation_evaluation, candidate.final_test_evaluation]
        for ev in evals:
            if ev is not None and (not ev.dataset_fingerprint or not ev.risk_config or not ev.execution_config):
                prov_missing.append(f"{ev.stage}:data/risk/execution provenance")
        report.add("provenance_complete", not prov_missing, f"missing: {prov_missing}")

        try:
            source = self._workspace.read_source(candidate_id)
        except OSError as exc:
            report.add("source_present", False, f"{type(exc).__name__}")
            return report
        report.add("source_present", True)
        report.add(
            "source_hash_matches",
            sha256_text(source) == candidate.source_hash,
            "candidate source no longer matches its recorded source_hash",
        )
        if candidate.approval:
            report.add(
                "approved_hashes_unchanged",
                candidate.approval.get("source_hash") == candidate.source_hash
                and candidate.approval.get("spec_hash") == candidate.spec_hash,
                "hashes differ from those the human approved",
            )
        report.add(
            "spec_hash_recorded",
            bool(re.fullmatch(r"[0-9a-f]{64}", candidate.spec_hash or "")),
            "spec_hash missing or malformed",
        )

        safety = validate_candidate_source(source)
        report.add("static_safety", safety.passed, "; ".join(safety.failed_checks))
        report.warnings.extend(safety.warnings)

        try:
            report.class_name = _find_class_name(source)
        except (SyntaxError, PromotionError) as exc:
            report.add("single_class", False, str(exc))

        # Platform-owned Strategy/Signal contract test, in the isolated runner.
        if safety.passed and report.class_name:
            result = self._runner(
                self._workspace.source_path(candidate_id),
                contract_candles(),
                CONTRACT_SYMBOL,
                dict(candidate.configuration),
            )
            ok = result.ok and all(
                isinstance(s, Signal)
                and isinstance(s.direction, SignalDirection)
                and s.symbol == CONTRACT_SYMBOL
                and 0.0 <= s.confidence <= 1.0
                for s in result.signals
            )
            report.add("strategy_signal_contract", ok, result.error or "emitted a malformed Signal")
            if result.ok and not result.signals:
                report.warnings.append("candidate emitted no signals on the contract candles")

        # Name resolution (collision check happens against live registry + manifest).
        proposed = _slug(strategy_name or candidate.name)
        report.strategy_name = proposed
        report.add("strategy_name_valid", bool(_NAME_RE.match(proposed)), f"{proposed!r} is not a valid strategy id")
        own = next((e for e in read_manifest(self._dir) if e.candidate_id == candidate_id), None)
        collision = proposed in self._names() and not (own and own.strategy_name == proposed)
        report.add("no_name_collision", not collision, f"a strategy named {proposed!r} is already registered")
        return report

    # ------------------------------------------------------------------
    # Promotion
    # ------------------------------------------------------------------

    def promote(
        self,
        candidate_id: str,
        *,
        human_approved: bool,
        approved_by: str = "human-cli",
        strategy_name: str | None = None,
    ) -> PromotionResult:
        """Run the full protocol. `human_approved` must be `True`: the
        CLI sets it only after the operator's explicit confirmation (or
        the explicit `--yes` flag); evidence validation is never skipped."""
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise PromotionError(f"no such candidate: {candidate_id!r}")

        if candidate.status == CandidateStrategyStatus.PROMOTED:
            if candidate.is_production_registered():
                entry = self._entry_for(candidate_id)
                if entry is None:
                    raise PromotionError(
                        f"candidate {candidate_id!r} is PROMOTED but the production manifest has no entry for it -- "
                        f"inconsistent state, refusing to guess"
                    )
                return PromotionResult(candidate, entry.strategy_name, entry, already_promoted=True)
            raise PromotionError(
                f"candidate {candidate_id!r} is a LEGACY PROMOTED record (promoted before ADR-0049, human-approved "
                f"but with no recorded production registration). It is not reinterpreted or back-filled. "
                f"If a strategy was registered by hand, its module docstring carries the lineage."
            )
        if candidate.status not in (CandidateStrategyStatus.REVIEW_REQUIRED, CandidateStrategyStatus.APPROVED):
            raise PromotionError(
                f"candidate {candidate_id!r} status is {candidate.status.value}; "
                f"must be REVIEW_REQUIRED (or APPROVED, to resume)"
            )
        if not human_approved:
            raise PromotionError("explicit human approval is required")

        report = self.preflight(candidate_id, strategy_name)
        if not report.passed:
            raise PromotionError("preflight failed: " + "; ".join(f"{n} ({d})" for n, d in report.failures))
        source = self._workspace.read_source(candidate_id)
        name = report.strategy_name
        assert name is not None and report.class_name is not None

        resumed = candidate.status == CandidateStrategyStatus.APPROVED
        if not resumed:
            candidate = self._candidates.approve(
                candidate_id,
                approved_by=approved_by,
                source_hash=candidate.source_hash,
                spec_hash=candidate.spec_hash,
            )
        approval = candidate.approval or {}

        # --- materialize (idempotent) --------------------------------
        entry = self._entry_for(candidate_id)
        created_file = created_entry = registered_here = False
        module_path = self._dir / f"{name}.py"
        if entry is not None:
            if entry.strategy_name != name or entry.source_hash != candidate.source_hash:
                raise PromotionError(
                    f"candidate {candidate_id!r} already has a conflicting manifest entry "
                    f"({entry.strategy_name!r}); refusing to overwrite"
                )
        else:
            if module_path.exists():
                raise PromotionError(f"refusing to overwrite existing production file {module_path.name}")
            entry = PromotedEntry(
                strategy_name=name,
                module=name,
                class_name=report.class_name,
                candidate_id=candidate_id,
                source_hash=candidate.source_hash,
                spec_hash=candidate.spec_hash,
                agent_run_id=candidate.agent_run_id,
                approved_at=str(approval.get("approved_at", "")),
                approved_by=str(approval.get("approved_by", approved_by)),
                registered_at=utc_now().isoformat(),
            )
            try:
                tmp = module_path.with_name(module_path.name + ".tmp")
                tmp.write_text(source, encoding="utf-8")
                tmp.replace(module_path)
                created_file = True
                write_manifest(read_manifest(self._dir) + [entry], self._dir)
                created_entry = True
            except BaseException as exc:
                self._rollback(entry, module_path, created_file, created_entry, False)
                raise PromotionError(f"materialization failed ({type(exc).__name__}: {exc}); candidate remains APPROVED") from exc

        # --- register + verify ---------------------------------------
        try:
            if name in self._names():
                cls = self._resolve(name)
                if getattr(cls, "__module__", "").rsplit(".", 1)[-1] != entry.module:
                    raise PromotionError(f"strategy id {name!r} is registered to a different strategy")
            else:
                cls = load_module_class(self._dir, entry)
                self._register(name)(cls)
                registered_here = True
            self._verify_registered(name, entry.class_name)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._rollback(entry, module_path, created_file, created_entry, registered_here)
            raise PromotionError(
                f"production registration failed ({type(exc).__name__}: {exc}); rolled back, candidate remains APPROVED"
            ) from exc

        # --- record PROMOTED last ------------------------------------
        registration = {
            "strategy_name": name,
            "module": f"src/strategies/promoted/{entry.module}.py",
            "class_name": entry.class_name,
            "module_sha256": sha256_text(module_path.read_text(encoding="utf-8")),
            "source_hash": candidate.source_hash,
            "registered_at": entry.registered_at,
            "manifest": "src/strategies/promoted/manifest.json",
        }
        try:
            promoted = self._candidates.promote(candidate_id, registration)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise PromotionError(
                f"strategy {name!r} IS registered (manifest entry present) but the candidate status update failed "
                f"({type(exc).__name__}: {exc}); candidate remains APPROVED. Re-run promotion for this candidate "
                f"to complete it -- it is idempotent."
            ) from exc
        return PromotionResult(promoted, name, entry, resumed=resumed)

    # ------------------------------------------------------------------

    def _entry_for(self, candidate_id: str) -> PromotedEntry | None:
        return next((e for e in read_manifest(self._dir) if e.candidate_id == candidate_id), None)

    def _verify_registered(self, name: str, class_name: str) -> None:
        cls = self._resolve(name)
        if cls.__name__ != class_name:
            raise PromotionError(f"registered class {cls.__name__!r} != expected {class_name!r}")
        instance = cls(symbol=CONTRACT_SYMBOL)
        if not isinstance(instance, Strategy):
            raise PromotionError("instance does not satisfy the Strategy contract")
        if instance.name != name:
            raise PromotionError(
                f"strategy.name is {instance.name!r} but registered id is {name!r}; ExperimentSpec "
                f"reconstruction needs them equal -- pass --strategy-name {instance.name!r}"
            )
        candles = contract_candles()
        signals = instance.generate_signals(instance.prepare(candles))
        for s in signals:
            if not (isinstance(s, Signal) and isinstance(s.direction, SignalDirection) and s.symbol == CONTRACT_SYMBOL):
                raise PromotionError("registered strategy emitted a non-canonical Signal")

    def _rollback(self, entry, module_path, created_file, created_entry, registered_here) -> None:
        if registered_here:
            try:
                self._unregister(entry.strategy_name)
            except KeyError:
                pass
        if created_entry:
            write_manifest([e for e in read_manifest(self._dir) if e.candidate_id != entry.candidate_id], self._dir)
        if created_file and module_path.exists():
            module_path.unlink()
        import sys

        sys.modules.pop(f"src.strategies.promoted.{entry.module}", None)
