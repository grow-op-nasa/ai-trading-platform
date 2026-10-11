"""Human-only candidate promotion CLI -- src/cli/strategy_promote.py.

    python -m src.cli strategy-promote --candidate-id <id> [--strategy-name <id>]

Sprint 14 spec, sections 57, 92-93 + ADR-0049: promotion is a deliberate
safety boundary and this module is its only entry point. It is never
imported by `src.ai.agents`, never registered as an `AgentTool`, and has
no code path reachable from the LLM.

**What "promoted" means (ADR-0049).** A human approval is not enough:
this command runs the full evidence/safety/contract preflight, asks for
explicit human approval, then -- through `src.promotion.PromotionService`
-- materializes the approved source into `src/strategies/promoted/`,
registers it with the production `StrategyRegistry`, verifies it
resolves, instantiates and emits canonical `Signal`s, and only then
marks the candidate `PROMOTED`. If any step fails the candidate stays
`APPROVED` (or `REVIEW_REQUIRED`), never a false `PROMOTED`.

Registration is not deployment: nothing here connects a broker, starts
paper trading, or places an order. The manifest/module changes it makes
under `src/strategies/promoted/` are ordinary files for you to review
and commit.
"""

from __future__ import annotations

import argparse
from typing import Sequence

from src.ai.agents.strategy_dev.models import CandidateStrategyStatus, registration_state
from src.ai.agents.strategy_dev.workspace import CandidateRegistry, CandidateWorkspace
from src.promotion import PromotionError, PromotionService


def _print_evidence(candidate) -> None:
    print(f"Candidate: {candidate.candidate_id}  ({candidate.name})")
    print(f"Status: {candidate.status.value}")
    print(f"Registration state: {registration_state(candidate)}")
    print(f"Description: {candidate.description}")
    print(f"Source hash: {candidate.source_hash}")
    print(f"Spec hash: {candidate.spec_hash}")
    print(f"Agent run: {candidate.agent_run_id}  ({candidate.provider}/{candidate.model})")
    if candidate.parent_candidate_id:
        print(f"Revises: {candidate.parent_candidate_id} -- {candidate.revision_reason}")
    for label, ev in (
        ("Development evaluation", candidate.development_evaluation),
        ("Validation evaluation", candidate.validation_evaluation),
    ):
        if ev is not None:
            print(f"{label}: trades={ev.trade_count}")
    ft = candidate.final_test_evaluation
    if ft is not None:
        print(
            f"Final out-of-sample evaluation: trades={ft.trade_count}  "
            f"period={ft.dataset_start} to {ft.dataset_end}"
        )
    else:
        print("Final out-of-sample evaluation: NONE RECORDED")


def run_strategy_promote(
    argv: Sequence[str] | None = None,
    *,
    service_factory=None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli strategy-promote")
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--strategy-name", help="production strategy id (default: slug of the candidate name)")
    parser.add_argument("--show-source", action="store_true", help="print the candidate's full source before promoting")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="explicit non-interactive approval (scripted use). Evidence/safety/contract validation still runs in full.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    candidates = CandidateRegistry()
    workspace = CandidateWorkspace()
    service = (
        service_factory(candidates, workspace)
        if service_factory is not None
        else PromotionService(candidates, workspace)
    )

    candidate = candidates.get(args.candidate_id)
    if candidate is None:
        print(f"strategy-promote: no such candidate: {args.candidate_id!r}")
        return 2
    _print_evidence(candidate)

    if candidate.status == CandidateStrategyStatus.PROMOTED:
        try:
            result = service.promote(args.candidate_id, human_approved=True)
        except PromotionError as exc:
            print(f"\nstrategy-promote: {exc}")
            return 1
        print(f"\nAlready promoted and registered as {result.strategy_name!r}; nothing to do.")
        return 0

    try:
        report = service.preflight(args.candidate_id, args.strategy_name)
    except PromotionError as exc:
        print(f"\nstrategy-promote: {exc}")
        return 1
    print(f"\nProposed production strategy id: {report.strategy_name}")
    print("Preflight checks:")
    for name, ok, detail in report.checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    for warning in report.warnings:
        print(f"  [WARN] {warning}")
    if not report.passed:
        print("\nstrategy-promote: refusing to promote -- preflight failed (nothing was changed).")
        return 1

    if args.show_source:
        print("\n----- candidate source -----")
        print(workspace.read_source(args.candidate_id))
        print("----- end candidate source -----")

    if not args.yes:
        answer = input(
            f"\nApprove candidate {args.candidate_id!r} and register it in the production "
            f"StrategyRegistry as {report.strategy_name!r}? This is a human decision only -- "
            f"the agent cannot make it. [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("strategy-promote: aborted, no change made.")
            return 1

    try:
        result = service.promote(
            args.candidate_id, human_approved=True, strategy_name=args.strategy_name
        )
    except PromotionError as exc:
        print(f"\nstrategy-promote: {exc}")
        refreshed = candidates.get(args.candidate_id)
        if refreshed is not None:
            print(f"Candidate status now: {refreshed.status.value} ({registration_state(refreshed)})")
        return 1

    cand = result.candidate
    print(f"\nPromoted and registered as {result.strategy_name!r}.")
    print(
        "Lineage: "
        f"candidate_id={cand.candidate_id} source_hash={cand.source_hash} "
        f"spec_hash={cand.spec_hash} agent_run_id={cand.agent_run_id} "
        f"approved_at={(cand.approval or {}).get('approved_at')}"
    )
    print(
        f"Wrote src/strategies/promoted/{result.entry.module}.py and updated "
        f"src/strategies/promoted/manifest.json -- review and commit them."
    )
    print(
        "\nNote: registration makes the strategy available through the normal StrategyRegistry for "
        "research. It does not deploy it, connect a broker, start paper trading, or place any order."
    )
    return 0
