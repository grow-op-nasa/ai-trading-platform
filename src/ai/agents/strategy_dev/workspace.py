"""Candidate workspace + registry -- src/ai/agents/strategy_dev/workspace.py.

Sprint 14 spec, sections 6, 90-91: candidate source must live entirely
outside `src/` (production source) and outside `tests/`. This module is
the *only* code in the platform that writes candidate source to disk --
the agent tool layer (`tools.py`) never touches a path directly; it
calls `CandidateWorkspace`/`CandidateRegistry` methods, which decide the
permitted location themselves (Sprint 14 spec, section 7: "the tool
itself determines the permitted filesystem location").

Layout, one directory per candidate under a single runtime root
(default `data/strategy_candidates/`, already covered by the repo's
existing wholesale `data/*` `.gitignore` rule -- the same convention
`src.ai.agents.store.AgentRunStore` already relies on for
`data/agent_runs/`, so no new `.gitignore` entry is needed):

    data/strategy_candidates/<candidate_id>/
        strategy.py
        manifest.json

`CandidateRegistry` is a small, separate, JSON-file-backed store for
candidate lifecycle/lineage/evaluation state -- deliberately not
`src.experiments.registry.ExperimentRegistry` (Sprint 14 spec, section
90: "it must not replace `ExperimentRegistry`... it is specifically for
unpublished strategy candidates") and never
`src.strategies.registry.StrategyRegistry` (section 91: candidates are
never registered there).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from src.ai.agents.strategy_dev.models import (
    CandidateEvaluation,
    CandidateStrategy,
    CandidateStrategySpec,
    CandidateStrategyStatus,
    IMMUTABLE_AFTER,
    utc_now,
)

DEFAULT_CANDIDATES_DIR = Path("data/strategy_candidates")

_TRANSITIONS: dict[CandidateStrategyStatus, tuple[CandidateStrategyStatus, ...]] = {
    CandidateStrategyStatus.DRAFT: (
        CandidateStrategyStatus.VALIDATING,
        CandidateStrategyStatus.REJECTED,
    ),
    CandidateStrategyStatus.VALIDATING: (
        CandidateStrategyStatus.VALIDATED,
        CandidateStrategyStatus.REJECTED,
    ),
    CandidateStrategyStatus.VALIDATED: (
        CandidateStrategyStatus.DEVELOPMENT_TESTED,
        CandidateStrategyStatus.REJECTED,
    ),
    CandidateStrategyStatus.DEVELOPMENT_TESTED: (
        CandidateStrategyStatus.FROZEN,
        CandidateStrategyStatus.REJECTED,
    ),
    CandidateStrategyStatus.FROZEN: (CandidateStrategyStatus.OUT_OF_SAMPLE_TESTED,),
    CandidateStrategyStatus.OUT_OF_SAMPLE_TESTED: (CandidateStrategyStatus.REVIEW_REQUIRED,),
    CandidateStrategyStatus.REVIEW_REQUIRED: (
        CandidateStrategyStatus.PROMOTED,
        CandidateStrategyStatus.REJECTED,
    ),
}


class CandidateImmutableError(Exception):
    """Raised by any attempt to mutate a candidate's source, spec, or
    configuration once it has passed the freeze gate (Sprint 14 spec,
    section 5)."""


class InvalidTransitionError(Exception):
    """Raised by any attempt to move a candidate's status somewhere
    `_TRANSITIONS` does not permit -- in particular, the one-way
    `FROZEN -> OUT_OF_SAMPLE_TESTED` edge can never be reversed (Sprint
    14 spec, section 36), and nothing transitions out of a terminal
    state (`REJECTED`/`PROMOTED`)."""


def sha256_text(text: str) -> str:
    """SHA-256 hex digest of `text` -- the identity primitive both
    `source_hash` (candidate code) and `spec_hash`
    (`CandidateStrategySpec.describe()`) are built from (Sprint 14
    spec, section 22)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CandidateWorkspace:
    """Owns the on-disk candidate source tree. The only code path that
    ever writes a `.py` file under `base_dir` (Sprint 14 spec, section
    7) -- the agent never receives a filesystem path to write to
    itself.

    Args:
        base_dir: defaults to `data/strategy_candidates/` (repo-root
            relative). Tests pass a `tmp_path` here to avoid touching
            the real workspace.
    """

    def __init__(self, base_dir: Path | str = DEFAULT_CANDIDATES_DIR) -> None:
        self._base_dir = Path(base_dir)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def candidate_dir(self, candidate_id: str) -> Path:
        return self._base_dir / candidate_id

    def write_source(self, candidate_id: str, source: str) -> Path:
        """Write `source` to this candidate's `strategy.py` -- only ever
        called once, at `DRAFT` creation time. Refuses to overwrite an
        existing file (candidates are immutable-by-convention from the
        moment they're written; a revision is a new `candidate_id`,
        Sprint 14 spec section 85)."""
        directory = self.candidate_dir(candidate_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "strategy.py"
        if path.exists():
            raise CandidateImmutableError(
                f"candidate {candidate_id!r} source already exists at {path} -- "
                f"candidates are immutable once written; create a new candidate to revise"
            )
        path.write_text(source, encoding="utf-8")
        return path

    def read_source(self, candidate_id: str) -> str:
        path = self.candidate_dir(candidate_id) / "strategy.py"
        return path.read_text(encoding="utf-8")

    def source_path(self, candidate_id: str) -> Path:
        return self.candidate_dir(candidate_id) / "strategy.py"


class CandidateRegistry:
    """A small, JSON-file-backed registry of `CandidateStrategy` records
    -- lifecycle, lineage, and evaluation state (Sprint 14 spec, section
    90). Explicitly not `ExperimentRegistry` and not `StrategyRegistry`.

    One JSON file per candidate under `base_dir` (mirrors
    `src.ai.agents.store.AgentRunStore`'s own one-file-per-record
    convention) -- simple, inspectable, and requires no new database
    dependency.

    Args:
        base_dir: defaults to `data/strategy_candidates/` -- the same
            root `CandidateWorkspace` uses (each candidate's `.py` and
            registry `.json` live side by side under
            `<base_dir>/<candidate_id>/`).
    """

    def __init__(self, base_dir: Path | str = DEFAULT_CANDIDATES_DIR) -> None:
        self._base_dir = Path(base_dir)

    def _record_path(self, candidate_id: str) -> Path:
        return self._base_dir / candidate_id / "manifest.json"

    def create(
        self,
        *,
        candidate_id: str,
        spec: CandidateStrategySpec,
        source: str,
        strategy_interface_version: str,
        agent_run_id: str,
        provider: str,
        model: str,
        system_prompt_version: str,
        tool_schema_version: str,
        policy_hash: str,
        parent_candidate_id: str | None = None,
        revision_reason: str | None = None,
        changes_summary: str | None = None,
    ) -> CandidateStrategy:
        candidate = CandidateStrategy(
            candidate_id=candidate_id,
            name=spec.strategy_name,
            description=spec.strategy_description,
            source_hash=sha256_text(source),
            spec_hash=sha256_text(json.dumps(spec.describe(), sort_keys=True)),
            strategy_interface_version=strategy_interface_version,
            created_at=utc_now(),
            created_by="agent",
            status=CandidateStrategyStatus.DRAFT,
            base_strategy=spec.base_strategy,
            configuration=dict(spec.parameters),
            feature_dependencies=tuple(spec.indicator_dependencies),
            agent_run_id=agent_run_id,
            provider=provider,
            model=model,
            system_prompt_version=system_prompt_version,
            tool_schema_version=tool_schema_version,
            policy_hash=policy_hash,
            parent_candidate_id=parent_candidate_id,
            revision_reason=revision_reason,
            changes_summary=changes_summary,
        )
        self._save(candidate)
        return candidate

    def get(self, candidate_id: str) -> CandidateStrategy | None:
        path = self._record_path(candidate_id)
        if not path.exists():
            return None
        return _from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_ids(self) -> list[str]:
        if not self._base_dir.exists():
            return []
        return sorted(
            p.parent.name for p in self._base_dir.glob("*/manifest.json") if p.is_file()
        )

    def transition(self, candidate_id: str, new_status: CandidateStrategyStatus) -> CandidateStrategy:
        """Move `candidate_id` to `new_status`, enforcing both the
        one-way lifecycle graph (`_TRANSITIONS`) and immutability after
        freeze (Sprint 14 spec, sections 5, 35-36).

        Raises:
            InvalidTransitionError: `new_status` is not a permitted
                successor of the candidate's current status.
        """
        candidate = self.get(candidate_id)
        if candidate is None:
            raise KeyError(f"no such candidate: {candidate_id!r}")
        allowed = _TRANSITIONS.get(candidate.status, ())
        if new_status not in allowed:
            raise InvalidTransitionError(
                f"cannot transition candidate {candidate_id!r} from "
                f"{candidate.status.value!r} to {new_status.value!r} "
                f"(allowed: {[s.value for s in allowed]})"
            )
        candidate.status = new_status
        self._save(candidate)
        return candidate

    def record_evaluation(
        self, candidate_id: str, stage: str, evaluation: CandidateEvaluation
    ) -> CandidateStrategy:
        """Attach `evaluation` to the `development`/`validation`/
        `final_test` slot on `candidate_id`.

        Raises:
            CandidateImmutableError: `stage == "final_test"` but the
                candidate has not yet reached `FROZEN` or later (Sprint
                14 spec, section 55: the agent must not receive final
                test performance before freeze), or the candidate
                already has a final test recorded (one evaluation only,
                Sprint 14 spec section 34's `max_final_test_evaluations`
                is enforced by the agent runtime; this is the
                complementary storage-level guarantee that a second
                final test can never silently overwrite the first).
        """
        candidate = self.get(candidate_id)
        if candidate is None:
            raise KeyError(f"no such candidate: {candidate_id!r}")
        if stage == "final_test":
            if candidate.status not in (
                CandidateStrategyStatus.FROZEN,
                CandidateStrategyStatus.OUT_OF_SAMPLE_TESTED,
            ):
                raise CandidateImmutableError(
                    f"candidate {candidate_id!r} must be FROZEN before a final test "
                    f"evaluation may be recorded (currently {candidate.status.value})"
                )
            if candidate.final_test_evaluation is not None:
                raise CandidateImmutableError(
                    f"candidate {candidate_id!r} already has a final test evaluation "
                    f"recorded -- the final test runs exactly once"
                )
            candidate.final_test_evaluation = evaluation
        elif stage == "validation":
            if candidate.status in IMMUTABLE_AFTER:
                raise CandidateImmutableError(
                    f"candidate {candidate_id!r} is immutable (status "
                    f"{candidate.status.value}) -- validation evaluations may only be "
                    f"recorded before freeze"
                )
            candidate.validation_evaluation = evaluation
        elif stage == "development":
            if candidate.status in IMMUTABLE_AFTER:
                raise CandidateImmutableError(
                    f"candidate {candidate_id!r} is immutable (status "
                    f"{candidate.status.value}) -- development evaluations may only be "
                    f"recorded before freeze"
                )
            candidate.development_evaluation = evaluation
        else:
            raise ValueError(f"unknown evaluation stage: {stage!r}")
        self._save(candidate)
        return candidate

    def reject(self, candidate_id: str, reason: str) -> CandidateStrategy:
        candidate = self.transition(candidate_id, CandidateStrategyStatus.REJECTED)
        candidate.rejection_reason = reason
        self._save(candidate)
        return candidate

    def promote(self, candidate_id: str) -> CandidateStrategy:
        """Move a candidate to `PROMOTED`. **Never called by the agent
        or any agent tool** (Sprint 14 spec, sections 57, 92) -- this
        method exists for `src.cli.strategy_promote` (a human-only CLI
        command) to call directly. No tool in `tools.py` wraps this
        method."""
        candidate = self.get(candidate_id)
        if candidate is None:
            raise KeyError(f"no such candidate: {candidate_id!r}")
        if candidate.status != CandidateStrategyStatus.REVIEW_REQUIRED:
            raise InvalidTransitionError(
                f"candidate {candidate_id!r} must be REVIEW_REQUIRED to promote "
                f"(currently {candidate.status.value}) -- full evidence "
                f"(validation + final test) must exist first"
            )
        if candidate.final_test_evaluation is None:
            raise InvalidTransitionError(
                f"candidate {candidate_id!r} has no final test evaluation -- cannot "
                f"promote without complete out-of-sample evidence"
            )
        return self.transition(candidate_id, CandidateStrategyStatus.PROMOTED)

    def _save(self, candidate: CandidateStrategy) -> None:
        path = self._record_path(candidate.candidate_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_to_dict(candidate), indent=2, sort_keys=True), encoding="utf-8")


def _to_dict(candidate: CandidateStrategy) -> dict:
    raw = asdict(candidate)
    raw["status"] = candidate.status.value
    raw["created_at"] = candidate.created_at.isoformat()
    for stage in ("development_evaluation", "validation_evaluation", "final_test_evaluation"):
        value = getattr(candidate, stage)
        raw[stage] = asdict(value) if value is not None else None
    return raw


def _from_dict(raw: dict) -> CandidateStrategy:
    from datetime import datetime

    raw = dict(raw)
    raw["status"] = CandidateStrategyStatus(raw["status"])
    raw["created_at"] = datetime.fromisoformat(raw["created_at"])
    for stage in ("development_evaluation", "validation_evaluation", "final_test_evaluation"):
        value = raw.get(stage)
        raw[stage] = CandidateEvaluation(**value) if value is not None else None
    raw["feature_dependencies"] = tuple(raw.get("feature_dependencies", ()))
    return CandidateStrategy(**raw)
