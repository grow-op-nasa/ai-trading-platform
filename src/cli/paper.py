"""`python -m src.cli paper ...` -- the explicit, bounded paper-trading commands (Sprint 18, ADR-0053).

    paper init       --config C --label L     record the paper broker's opening baseline (explicit, once)
    paper preflight  --config C               read-only readiness report (PASS / FAIL / BLOCKED / NOT_EVALUABLE)
    paper run --once --config C               ONE decision cycle (there is no loop and no daemon)
    paper reconcile  --config C               re-run the paper-broker vs portfolio reconciliation
    paper status     --config C [--cycles|--blocked|--intents|--discrepancies|--fills]   read-only inspection

Paper only: the runtime is hard-bound to `PaperBroker`; no flag or environment variable selects anything else.
Nothing starts unless one of these commands is run. A successful exit never implies a fill -- read `status`.

`paper run` exit codes
    0  completed: NO_NEW_BAR, NO_ACTION, RISK_REJECTED or ORDER_FILLED (the JSON `status` says which)
    1  completed but needs attention: AWAITING_EXECUTION, ORDER_REJECTED, RECONCILIATION_REQUIRED
    2  configuration / setup error (nothing recorded)
    3  BLOCKED by data or safety preconditions, or another paper run holds the lock
    4  FAILED (strategy, risk-engine, persistence or infrastructure error)
`paper preflight`: 0 PASS, 1 NOT_EVALUABLE only, 2 FAIL, 3 BLOCKED.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Sequence

from src.paper.config import PaperConfigError, PaperRunConfig
from src.paper.models import CheckStatus, CycleStatus
from src.paper.runtime import PaperLockedError, PaperProposalError, PaperSetupError, PaperTradingRuntime
from src.paper.store import DEFAULT_DB_PATH as DEFAULT_PAPER_DB
from src.snapshots import SnapshotStore
from src.paper.store import PaperStore, PaperStoreError

EXIT_OK, EXIT_ATTENTION, EXIT_CONFIG, EXIT_BLOCKED, EXIT_FAILED = 0, 1, 2, 3, 4

_RUN_EXIT = {
    CycleStatus.NO_NEW_BAR: EXIT_OK, CycleStatus.NO_ACTION: EXIT_OK, CycleStatus.RISK_REJECTED: EXIT_OK,
    CycleStatus.ORDER_FILLED: EXIT_OK, CycleStatus.AWAITING_EXECUTION: EXIT_ATTENTION,
    CycleStatus.ORDER_REJECTED: EXIT_ATTENTION, CycleStatus.RECONCILIATION_REQUIRED: EXIT_ATTENTION,
    CycleStatus.BLOCKED: EXIT_BLOCKED, CycleStatus.FAILED: EXIT_FAILED, CycleStatus.STARTED: EXIT_FAILED,
    CycleStatus.AWAITING_APPROVAL: EXIT_ATTENTION, CycleStatus.PROPOSAL_REJECTED: EXIT_OK,
    CycleStatus.PROPOSAL_EXPIRED: EXIT_ATTENTION, CycleStatus.PROPOSAL_INVALIDATED: EXIT_ATTENTION,
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="paper", description="Deterministic paper-only trading runtime (no live broker)")
    sub = p.add_subparsers(dest="command")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", required=True, help="path to the explicit paper-run JSON configuration")
        sp.add_argument("--registry", default=None, help="experiment registry (default data/experiments.db)")
        sp.add_argument("--paper-db", default=None, help=f"paper runtime database (default {DEFAULT_PAPER_DB})")
        sp.add_argument("--json", action="store_true", help="machine-readable output")

    sp = sub.add_parser("init", help="record the paper account baseline from the current saved portfolio")
    common(sp)
    sp.add_argument("--label", required=True)
    sp = sub.add_parser("preflight", help="read-only readiness checks")
    common(sp)
    sp.add_argument("--no-data-check", action="store_true", help="skip the market-data availability check")
    sp = sub.add_parser("run", help="execute decision cycle(s)")
    common(sp)
    sp.add_argument("--once", action="store_true", help="required: run exactly one cycle (there is no loop)")
    sp.add_argument("--monitor-db", default=None, help="monitoring database (default data/monitoring/monitoring.db)")
    sp.add_argument("--no-monitoring", action="store_true", help="skip the post-cycle monitoring run")
    sp = sub.add_parser("proposals", help="list trade proposals awaiting a human decision (read-only)")
    common(sp)
    sp = sub.add_parser("approve", help="approve ONE persisted proposal (re-validated; then one paper order)")
    common(sp)
    sp.add_argument("--proposal", required=True, help="the exact proposal id")
    sp.add_argument("--monitor-db", default=None)
    sp.add_argument("--no-monitoring", action="store_true")
    sp = sub.add_parser("reject", help="reject ONE persisted proposal (no order)")
    common(sp)
    sp.add_argument("--proposal", required=True)
    sp = sub.add_parser("reconcile", help="re-run paper-broker vs portfolio reconciliation")
    common(sp)
    sp = sub.add_parser("status", help="read-only inspection")
    common(sp)
    sp.add_argument("--cycles", action="store_true")
    sp.add_argument("--blocked", action="store_true")
    sp.add_argument("--intents", action="store_true")
    sp.add_argument("--discrepancies", action="store_true")
    sp.add_argument("--fills", action="store_true")
    sp.add_argument("--limit", type=int, default=10)
    return p


def _default_registry() -> str:
    from src.experiments.registry import DEFAULT_DB_PATH
    return str(DEFAULT_DB_PATH)


def _default_monitor_factory(cfg: PaperRunConfig, registry_path: str, monitor_db: str | None) -> Callable[[], Any]:
    def factory():
        from src.cli.monitor import _default_service
        return _default_service(cfg.experiment_id, monitor_db, registry_path)
    return factory


def run_paper(argv: Sequence[str] | None = None, *, market_data: Any = None, monitor_factory: Callable | None = None,
              clock: Callable | None = None, emit: Callable[[str], None] = print,
              allowed_strategy_prefixes: tuple[str, ...] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return EXIT_CONFIG if exc.code else EXIT_OK
    if not args.command:
        parser.print_usage()
        return EXIT_CONFIG
    try:
        cfg = PaperRunConfig.from_file(args.config)
    except PaperConfigError as exc:
        emit(f"paper: configuration error: {exc}")
        return EXIT_CONFIG
    registry = args.registry or _default_registry()
    paper_db = Path(args.paper_db) if args.paper_db else DEFAULT_PAPER_DB

    if market_data is None and args.command in ("preflight", "run", "approve"):
        from src.data.service import MarketDataService
        market_data = MarketDataService()
    mon = None
    if args.command in ("preflight", "run", "approve") and not getattr(args, "no_monitoring", False):
        mon = monitor_factory or _default_monitor_factory(cfg, registry, getattr(args, "monitor_db", None))
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    if allowed_strategy_prefixes is not None:      # test seam only; there is no CLI flag or environment variable for it
        kwargs["allowed_strategy_prefixes"] = allowed_strategy_prefixes
    # Each cycle's dataset is preserved (ADR-0059): the shared data/snapshots for the default database, otherwise a
    # `snapshots` directory beside the chosen database (so a disposable --paper-db keeps its evidence with it).
    snap_dir = Path("data/snapshots") if not args.paper_db else paper_db.parent / "snapshots"
    runtime = PaperTradingRuntime(cfg, registry_path=registry, store_path=paper_db, market_data=market_data,
                                  monitor_factory=mon, snapshots=SnapshotStore(snap_dir), **kwargs)

    try:
        if args.command == "init":
            info = runtime.initialize_account(args.label)
            emit(json.dumps(info, indent=2) if args.json else
                 f"paper account initialised for experiment {info['experiment_id']}: baseline cash "
                 f"{info['baseline_cash']:.2f}, {len(info['baseline_positions'])} open position(s)")
            return EXIT_OK
        if args.command == "preflight":
            rep = runtime.preflight(check_data=not args.no_data_check)
            if args.json:
                emit(json.dumps(rep.to_dict(), indent=2))
            else:
                for c in rep.checks:
                    emit(f"  [{c.status.value:<13}] {c.name}: {c.detail}")
                emit(f"preflight: {rep.overall.value} (ready={rep.ready}); read-only, nothing was submitted or changed")
            return {CheckStatus.PASS: EXIT_OK, CheckStatus.NOT_EVALUABLE: EXIT_ATTENTION,
                    CheckStatus.FAIL: EXIT_CONFIG, CheckStatus.BLOCKED: EXIT_BLOCKED}[rep.overall]
        if args.command == "run":
            if not args.once:
                emit("paper run: --once is required (exactly one bounded decision cycle; there is no loop)")
                return EXIT_CONFIG
            rep = runtime.preflight(check_data=False)
            hard = [c for c in rep.checks if c.status is CheckStatus.FAIL]
            if hard:
                for c in hard:
                    emit(f"paper run: preflight FAIL {c.name}: {c.detail}")
                return EXIT_CONFIG
            rec = runtime.run_cycle()
            if rec.status is CycleStatus.AWAITING_APPROVAL and not args.json:
                emit("  a trade PROPOSAL awaits your decision; NO order exists. "
                     "Review it with `paper proposals`, then `paper approve --proposal <id>` or `paper reject --proposal <id>`.")
            payload = rec.to_dict()
            payload["exit_code"] = _RUN_EXIT[rec.status]
            payload["filled"] = rec.status is CycleStatus.ORDER_FILLED
            payload["executed_by"] = "PaperBroker (simulated; not a live or real-market fill)"
            if args.json:
                emit(json.dumps(payload, indent=2, default=str))
            else:
                emit(f"cycle {rec.cycle_id}: {rec.status.value}" + (f" [{rec.reason_code}]" if rec.reason_code else ""))
                if rec.reason:
                    emit(f"  {rec.reason}")
                if rec.decision_bar:
                    emit(f"  decision bar: {rec.decision_bar}   replayed: {rec.replayed}")
                if rec.client_order_id:
                    emit(f"  intent {rec.intent_id}  client order {rec.client_order_id}")
                if rec.monitoring:
                    emit(f"  monitoring: {rec.monitoring}")
                emit("  (a zero exit does not mean a trade filled -- see the status above)")
            return _RUN_EXIT[rec.status]
        if args.command == "proposals":
            props = runtime.list_proposals()
            if args.json:
                emit(json.dumps([p.to_dict() for p in props], indent=2, default=str))
            else:
                for p in props:
                    emit(f"  {p.proposal_id} {p.status.value:<11} {p.side} {p.quantity:g} {p.symbol} ref {p.reference_price:g} "
                         f"bar {p.decision_bar} expires {p.expires_at}")
                emit(f"{len(props)} proposal(s); listing is read-only")
            return EXIT_OK
        if args.command in ("approve", "reject"):
            try:
                rec = (runtime.approve_proposal(args.proposal) if args.command == "approve"
                       else runtime.reject_proposal(args.proposal, "rejected from the command line"))
            except PaperProposalError as exc:
                emit(f"paper {args.command}: refused [{exc.code}] {exc}; nothing was submitted")
                return EXIT_BLOCKED
            emit(json.dumps(rec.to_dict(), indent=2, default=str) if args.json
                 else f"cycle {rec.cycle_id}: {rec.status.value}" + (f" [{rec.reason_code}]" if rec.reason_code else ""))
            return _RUN_EXIT[rec.status]
        if args.command == "reconcile":
            rec = runtime.reconcile_now()
            emit(json.dumps(rec.to_dict(), indent=2, default=str) if args.json
                 else f"reconciliation {rec.recon_id}: {rec.status.value}; {len(rec.discrepancies)} discrepancy(ies)")
            return EXIT_OK if rec.status.value == "MATCH" else EXIT_ATTENTION
        if args.command == "status":
            return _status(args, cfg, paper_db, registry, emit)
    except (PaperSetupError, PaperStoreError) as exc:
        emit(f"paper: {exc}")
        return EXIT_CONFIG
    except PaperLockedError as exc:
        emit(f"paper: {exc}")
        return EXIT_BLOCKED
    emit("paper: unknown command")
    return EXIT_CONFIG


def _status(args, cfg: PaperRunConfig, paper_db: Path, registry: str, emit) -> int:
    store = PaperStore.open_existing(paper_db)            # never creates; raises PaperStoreError if missing
    exp = cfg.experiment_id
    out: dict[str, Any] = {"experiment_id": exp, "account": store.get_account(exp)}
    focus = any((args.cycles, args.blocked, args.intents, args.discrepancies, args.fills))
    if args.cycles or not focus:
        out["cycles"] = [c.to_dict() for c in store.list_cycles(exp, args.limit)]
    if args.blocked:
        bad = (CycleStatus.BLOCKED, CycleStatus.FAILED, CycleStatus.RECONCILIATION_REQUIRED,
               CycleStatus.AWAITING_EXECUTION, CycleStatus.ORDER_REJECTED)
        out["problem_cycles"] = [c.to_dict() for c in store.list_cycles(exp, args.limit, bad)]
    if args.intents or not focus:
        out["intents"] = [i.to_dict() for i in store.list_intents(exp, limit=args.limit)]
        out["unresolved_intents"] = [i.client_order_id for i in store.list_intents(exp, unresolved_only=True)]
    if args.fills:
        out["fills"] = [f.to_dict() for f in store.list_fills(exp)[-args.limit:]]
    if args.discrepancies or not focus:
        out["reconciliations"] = [r.to_dict() for r in store.list_reconciliations(exp, args.limit,
                                                                                  only_problems=args.discrepancies)]
    if args.json:
        emit(json.dumps(out, indent=2, default=str))
        return EXIT_OK
    emit(f"experiment {exp}: account {'initialised' if out['account'] else 'MISSING'}")
    for c in out.get("cycles", []) + out.get("problem_cycles", []):
        emit(f"  cycle {c['cycle_id'][:12]} {c['status']:<24} bar={c['decision_bar']} {c.get('reason_code') or ''}")
    for i in out.get("intents", []):
        emit(f"  intent {i['client_order_id']} {i['state']} {i['side']} {i['quantity']:g} {i['symbol']}")
    for f in out.get("fills", []):
        emit(f"  fill {f['client_order_id']} {f['side']} {f['quantity']:g} @ {f['fill_price']:g} fee {f['fee']:g}")
    for r in out.get("reconciliations", []):
        emit(f"  reconciliation {r['recon_id']} {r['status']} discrepancies={len(r['discrepancies'])}")
    if out.get("unresolved_intents"):
        emit(f"  UNRESOLVED intents: {out['unresolved_intents']}")
    return EXIT_OK
