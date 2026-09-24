"""Human-only candidate promotion CLI -- src/cli/strategy_promote.py.

    python -m src.cli strategy-promote --candidate-id <id>

Sprint 14 spec, sections 57, 92-93: promotion is a deliberate safety
boundary. This module is the *only* caller of
`CandidateRegistry.promote()` in the entire platform besides the
registry's own tests -- it is never imported by
`src.ai.agents.strategy_dev.tools`, never registered as an
`AgentTool`, and has no code path reachable from the LLM. A human runs
this command directly, after reviewing the candidate's evidence and
source themselves (`inspect_candidate`/`get_candidate_report` output,
or by reading `data/strategy_candidates/<id>/strategy.py` directly).

Promotion here only marks the candidate `PROMOTED` in the
`CandidateRegistry` and prints its production-identity lineage
(candidate_id, source_hash, spec_hash, agent_run_id, and this
approval's timestamp) for the human to record -- it does not register
anything in `src.strategies.registry.StrategyRegistry`, deploy
anything, or place any trade (Sprint 14 spec, sections 96-98: "no
automatic deployment").
"""

from __future__ import annotations

import argparse
from typing import Sequence

from src.ai.agents.strategy_dev.models import CandidateStrategyStatus, utc_now
from src.ai.agents.strategy_dev.workspace import (
    CandidateRegistry,
    CandidateWorkspace,
    InvalidTransitionError,
)


def run_strategy_promote(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli strategy-promote")
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument(
        "--show-source",
        action="store_true",
        help="print the candidate's full source to the terminal before promoting",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation prompt (for scripted use only)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    registry = CandidateRegistry()
    workspace = CandidateWorkspace()

    candidate = registry.get(args.candidate_id)
    if candidate is None:
        print(f"strategy-promote: no such candidate: {args.candidate_id!r}")
        return 2

    print(f"Candidate: {candidate.candidate_id}  ({candidate.name})")
    print(f"Status: {candidate.status.value}")
    print(f"Description: {candidate.description}")
    print(f"Source hash: {candidate.source_hash}")
    print(f"Spec hash: {candidate.spec_hash}")
    print(f"Agent run: {candidate.agent_run_id}  ({candidate.provider}/{candidate.model})")
    if candidate.parent_candidate_id:
        print(f"Revises: {candidate.parent_candidate_id} -- {candidate.revision_reason}")

    if candidate.development_evaluation is not None:
        print(f"\nDevelopment evaluation: trades={candidate.development_evaluation.trade_count}")
    if candidate.validation_evaluation is not None:
        print(f"Validation evaluation: trades={candidate.validation_evaluation.trade_count}")
    if candidate.final_test_evaluation is not None:
        print(
            f"Final out-of-sample evaluation: trades="
            f"{candidate.final_test_evaluation.trade_count}  "
            f"period={candidate.final_test_evaluation.dataset_start} to "
            f"{candidate.final_test_evaluation.dataset_end}"
        )
    else:
        print("Final out-of-sample evaluation: NONE RECORDED")

    if candidate.status != CandidateStrategyStatus.REVIEW_REQUIRED:
        print(
            f"\nstrategy-promote: refusing to promote -- candidate status is "
            f"{candidate.status.value!r}, must be REVIEW_REQUIRED (full validation "
            f"and an untouched final out-of-sample test are required first)."
        )
        return 1
    if candidate.final_test_evaluation is None:
        print(
            "\nstrategy-promote: refusing to promote -- no final out-of-sample "
            "evaluation is recorded on this candidate."
        )
        return 1

    if args.show_source:
        print("\n----- candidate source -----")
        print(workspace.read_source(args.candidate_id))
        print("----- end candidate source -----")

    if not args.yes:
        answer = input(
            f"\nPromote candidate {args.candidate_id!r} to PROMOTED? This is a "
            f"human decision only -- the agent cannot make it. [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("strategy-promote: aborted, no change made.")
            return 1

    try:
        promoted = registry.promote(args.candidate_id)
    except InvalidTransitionError as exc:
        print(f"strategy-promote: {exc}")
        return 1

    approved_at = utc_now().isoformat()
    print(f"\nPromoted at {approved_at}.")
    print(
        "Lineage for the production record: "
        f"candidate_id={promoted.candidate_id} source_hash={promoted.source_hash} "
        f"spec_hash={promoted.spec_hash} agent_run_id={promoted.agent_run_id} "
        f"approved_at={approved_at}"
    )
    print(
        "\nNote: this command only marks the candidate PROMOTED in the candidate "
        "registry. It does not register the strategy in the production "
        "StrategyRegistry, deploy it, or place any trade -- that remains a "
        "separate, explicit step outside this sprint's scope."
    )
    return 0
