"""Opportunity Scanner service (Sprint 21, ADR-0058). Streamlit-free.

Watchlist rules live here; scanning delegates to `src.scanner.engine`. NOTHING here places an order or creates an intent,
cycle, proposal or fill. The only handoff to the paper workflow is `review_handoff`, which returns a small descriptor the UI
stores in session state; the paper workflow then runs its own fresh preflight, risk check and human approval.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from src.appsvc.instruments import normalize_symbol
from src.appsvc.paths import AppPaths
from src.snapshots import SnapshotStore
from src.scanner.engine import RiskContext, ScanEngine, ScanSetupError
from src.scanner.models import (DEFAULT_CONCURRENCY, MAX_CONCURRENCY, MAX_WATCHLIST_SYMBOLS, STARTER_NAME, STARTER_SYMBOLS,
                                ResultStatus, RiskState, ScanRecord, ScanResult, ScanStatus, Watchlist)
from src.scanner.store import DuplicateWatchlistName, ScannerStore, ScannerStoreError

__all__ = ["ScannerService", "WatchlistError", "ScanSetupError", "ScanView", "MAX_WATCHLIST_SYMBOLS", "STATUS_MEANING",
           "explain_result", "trace_of", "parse_symbols", "ResultStatus", "ScanStatus", "RiskState"]

MAX_NAME = 60


class WatchlistError(ValueError):
    """A watchlist edit that was refused. Nothing was changed."""


STATUS_MEANING = {
    ResultStatus.SIGNAL_FOUND: "The strategy produced a signal at the latest completed daily bar.",
    ResultStatus.NO_SIGNAL: "Evaluated successfully; the strategy has nothing actionable right now.",
    ResultStatus.RISK_BLOCKED: "A signal was found but the existing risk rules would reject it.",
    ResultStatus.DATA_UNAVAILABLE: "The data provider could not supply data. This is NOT 'no signal'.",
    ResultStatus.NOT_EVALUABLE: "Data arrived but cannot support a valid evaluation (stale, incomplete, too short or failed integrity checks). This is NOT 'no signal'.",
    ResultStatus.ERROR: "An unexpected failure for this symbol. Other symbols were not affected.",
}


def parse_symbols(text: str) -> tuple[list[str], list[str]]:
    """Split free text on commas/whitespace/newlines -> (normalised, de-duplicated symbols, problems). Never drops silently."""
    out: list[str] = []
    problems: list[str] = []
    for tok in [t for t in (text or "").replace(",", " ").replace(";", " ").split() if t]:
        sym, why = normalize_symbol(tok)
        if sym is None:
            problems.append(f"{tok!r}: {why}")
        elif sym not in out:
            out.append(sym)
    return out, problems


@dataclass
class ScanView:
    scan: ScanRecord
    status: ScanStatus                  # effective (a RUNNING record whose owner is gone reads INTERRUPTED)
    results: list[ScanResult]


def trace_of(r: ScanResult) -> dict[str, Any] | None:
    """The audit trace recorded at scan time, or None for scans made before it existed (it is never reconstructed later)."""
    t = (r.data or {}).get("trace")
    return t if isinstance(t, dict) else None


def reproducibility_of(scan: ScanRecord, r: ScanResult) -> dict[str, Any]:
    """STORED facts about how reproducible a result is (no replay is run here): the history assessment and snapshot reference
    recorded at scan time, with an explicit label for results that predate them. Never reconstructed after the fact."""
    d = r.data or {}
    snap = d.get("snapshot")
    hist = ((d.get("trace") or {}).get("history")) or None
    legacy = scan.history_status == "LEGACY_HEURISTIC"
    return {
        "history": hist if (hist and hist.get("status")) else None,
        "history_note": ("Recorded before strategies declared history requirements (the retired scanner heuristic was used)."
                         if legacy else None),
        "snapshot": snap if snap else None,
        "replayable": bool(snap and snap.get("status") == "CAPTURED"),
        "label": ("Stored snapshot available: an offline replay can be requested." if snap and snap.get("status") == "CAPTURED"
                  else "Not replayable: the input data of this result was not preserved (legacy scan, no data obtained, or no snapshot store)."),
        "environment": d.get("environment"),
    }


def explain_result(r: ScanResult) -> list[str]:
    """Plain-English lines built only from stored facts: no scores, no invented confidence."""
    lines = [STATUS_MEANING[r.status]]
    if r.reason and r.reason not in lines:
        lines.append(r.reason)
    if r.status is ResultStatus.NO_SIGNAL and not r.decision:
        lines.append("Strategies here emit a signal only when their state CHANGES. 'No signal' means nothing changed on the decision bar; "
                     "it does not say the symbol is bullish, bearish or neutral.")
    if r.reason_code == "ALREADY_FLAT" and r.decision:
        lines.append(f"The strategy DID emit {r.decision.get('direction')} on the decision bar, but the selected account holds no position in {r.symbol}, "
                     "so there is nothing to close and no action to review. It is shown as 'No signal' only for that reason.")
    if r.decision:
        lines.append(f"Strategy output: {r.decision.get('direction')} on bar {r.decision.get('timestamp')}.")
    if r.decision_bar:
        lines.append(f"Decision bar: {r.decision_bar}.")
    if r.risk_state is RiskState.NOT_ASSESSED:
        lines.append("Risk: NOT assessed in this scan. Use the paper workflow, whose fresh preflight and risk check decide.")
    elif r.risk_state is RiskState.APPROVED:
        lines.append("Risk: the existing risk engine approved this candidate on its own against the saved portfolio. "
                     "That is not a promise: a fresh preflight and your approval still apply.")
    elif r.risk_state is RiskState.NOT_APPLICABLE:
        lines.append("Risk: nothing to assess (there is no tradeable action here). This is NOT a risk approval.")
    elif r.risk_state is RiskState.REJECTED:
        lines.append(f"Risk: rejected. {r.risk_detail}")
    d = r.data or {}
    if d.get("provider"):
        lines.append(f"Data: {d.get('bars')} bars from {d['provider']} (hash {str(d.get('content_hash'))[:12]}).")
    return lines


class ScannerService:
    def __init__(self, paths: AppPaths | None = None, *, market_data: Any = None, paper_service: Any = None,
                 clock: Callable | None = None, calendar: Any = None, concurrency: int = DEFAULT_CONCURRENCY,
                 allowed_strategy_prefixes: tuple[str, ...] = ("src.strategies",)) -> None:
        self.paths = paths or AppPaths()
        self._market_data = market_data
        self._paper = paper_service
        self._clock, self._calendar, self._conc, self._prefixes = clock, calendar, concurrency, allowed_strategy_prefixes
        self.store = ScannerStore(self.paths.scanner_db)
        self.snapshots = SnapshotStore(self.paths.snapshots_dir)
        self._run_lock = threading.Lock()

    # ------------------------------------------------------------------ watchlists
    def ensure_starter(self) -> Watchlist | None:
        """Seed the documented starter list only when the store has never held a watchlist. A WRITE: call from an explicit
        action, never from a render. Returns the new list, or None if nothing was needed."""
        return self.store.seed_starter(STARTER_NAME, STARTER_SYMBOLS)

    def watchlists(self) -> list[Watchlist]:
        return self.store.list_watchlists()

    def watchlist(self, watchlist_id: str) -> Watchlist | None:
        return self.store.get_watchlist(watchlist_id)

    @staticmethod
    def _clean_name(name: str) -> str:
        n = " ".join((name or "").split())
        if not n:
            raise WatchlistError("Give the watchlist a name.")
        if len(n) > MAX_NAME:
            raise WatchlistError(f"Watchlist names are limited to {MAX_NAME} characters.")
        return n

    @staticmethod
    def _clean_symbols(symbols: list[str] | str) -> tuple[str, ...]:
        if isinstance(symbols, str):
            syms, problems = parse_symbols(symbols)
        else:
            syms, problems = [], []
            for s in symbols:
                sym, why = normalize_symbol(s)
                if sym is None:
                    problems.append(f"{s!r}: {why}")
                elif sym not in syms:
                    syms.append(sym)
        if problems:
            raise WatchlistError("Some symbols were not accepted, so nothing was saved: " + "; ".join(problems))
        if not syms:
            raise WatchlistError("Add at least one ticker symbol.")
        if len(syms) > MAX_WATCHLIST_SYMBOLS:
            raise WatchlistError(f"A watchlist can hold at most {MAX_WATCHLIST_SYMBOLS} symbols; this has {len(syms)}. "
                                 "Nothing was truncated or saved. Remove some symbols or split it into two watchlists.")
        return tuple(syms)

    def create_watchlist(self, name: str, symbols: list[str] | str) -> Watchlist:
        n, s = self._clean_name(name), self._clean_symbols(symbols)
        try:
            return self.store.create_watchlist(n, s)
        except DuplicateWatchlistName as exc:
            raise WatchlistError(str(exc)) from exc

    def update_watchlist(self, watchlist_id: str, *, name: str | None = None, symbols: list[str] | str | None = None) -> Watchlist:
        n = self._clean_name(name) if name is not None else None
        s = self._clean_symbols(symbols) if symbols is not None else None
        try:
            return self.store.update_watchlist(watchlist_id, name=n, symbols=s)
        except DuplicateWatchlistName as exc:
            raise WatchlistError(str(exc)) from exc
        except ScannerStoreError as exc:
            raise WatchlistError(str(exc)) from exc

    def delete_watchlist(self, watchlist_id: str) -> bool:
        """Deleting a watchlist never deletes scan history: past scans keep their own snapshot of the symbols."""
        return self.store.delete_watchlist(watchlist_id)

    # ------------------------------------------------------------------ scanning
    def _md(self):
        if self._market_data is None:
            from src.data.service import MarketDataService
            self._market_data = MarketDataService()
        return self._market_data

    def _engine(self) -> ScanEngine:
        kw: dict[str, Any] = {"concurrency": self._conc, "allowed_strategy_prefixes": self._prefixes}
        if self._clock is not None:
            kw["clock"] = self._clock
        if self._calendar is not None:
            kw["calendar"] = self._calendar
        return ScanEngine(self.store, self._md(), snapshots=self.snapshots, **kw)

    def paper_service(self):
        if self._paper is None:
            from src.appsvc.paper import PaperService
            self._paper = PaperService(self.paths, market_data=self._market_data)
        return self._paper

    def _risk_context(self, account: int, strategy: str, params: dict, symbol: str) -> RiskContext:
        from src.experiments.registry import ExperimentRegistry
        from src.risk import PortfolioRiskEngine
        cfg = self.paper_service().build_config(experiment_id=account, strategy_name=strategy, symbol=symbol, strategy_params=params)
        portfolio = ExperimentRegistry(self.paths.registry).get_portfolio(account) if self.paths.registry_exists() else None
        if portfolio is None:
            raise ScanSetupError(f"account {account} has no saved portfolio, so risk cannot be assessed")
        return RiskContext(PortfolioRiskEngine(cfg.risk_limits(), cfg.portfolio_limits()), cfg.stop_policy(), portfolio)

    def start_scan(self, watchlist_id: str, strategy_name: str, params: dict | None = None, *, account: int | None = None,
                   assess_risk: bool = False, request_token: str | None = None) -> tuple[ScanRecord, bool]:
        wl = self.store.get_watchlist(watchlist_id)
        if wl is None:
            raise ScanSetupError("that watchlist no longer exists")
        if assess_risk and account is None:
            raise ScanSetupError("choose an account to assess risk against, or turn risk assessment off")
        return self._engine().start(wl, strategy_name, params, account=account, risk_enabled=assess_risk, request_token=request_token)

    def run_scan(self, scan: ScanRecord, progress: Callable[[int, int, str], None] | None = None) -> ScanRecord:
        """Execute a scan created by `start_scan` (blocking). Safe to call once per created scan."""
        risk, note = None, None
        if scan.risk_mode == "ASSESSED" and scan.account is not None:
            try:
                risk = self._risk_context(scan.account, scan.strategy_name, scan.strategy_params, scan.symbols[0])
            except Exception as exc:  # noqa: BLE001 - fall back to an honest NOT_ASSESSED, never a guess
                from src.paper.evaluation import safe_error_text
                note = f"Risk could not be set up ({safe_error_text(exc)}); signals are reported as NOT assessed."
        return self._engine().execute(scan, risk=risk, progress=progress, note=note)

    def scan(self, watchlist_id: str, strategy_name: str, params: dict | None = None, *, account: int | None = None,
             assess_risk: bool = False, request_token: str | None = None,
             progress: Callable[[int, int, str], None] | None = None) -> tuple[ScanRecord, bool]:
        rec, created = self.start_scan(watchlist_id, strategy_name, params, account=account, assess_risk=assess_risk,
                                       request_token=request_token)
        if not created:
            return rec, False
        return self.run_scan(rec, progress), True

    # ------------------------------------------------------------------ replay (read-only, offline)
    def replay(self, scan_id: str, progress: Callable[[int, int, str], None] | None = None):
        """Re-evaluate a stored scan from its stored snapshots. No provider call, no write, no order/intent/proposal."""
        from src.scanner.replay import ReplayService
        kw: dict[str, Any] = {"allowed_strategy_prefixes": self._prefixes}
        if self._calendar is not None:
            kw["calendar"] = self._calendar
        return ReplayService(self.store, self.snapshots, **kw).replay_scan(scan_id, progress=progress)

    # ------------------------------------------------------------------ history
    def history(self, limit: int = 50) -> list[ScanView]:
        eng = self._engine()
        return [ScanView(s, eng.effective_status(s), []) for s in self.store.list_scans(limit)]

    def view(self, scan_id: str) -> ScanView | None:
        rec = self.store.get_scan(scan_id)
        if rec is None:
            return None
        return ScanView(rec, self._engine().effective_status(rec), self.store.results(scan_id))

    # ------------------------------------------------------------------ handoff (no mutation)
    @staticmethod
    def review_handoff(scan: ScanRecord, result: ScanResult) -> dict[str, Any]:
        """Descriptor for 'Review in paper workflow'. It is data only: the paper workflow re-runs preflight, risk and approval."""
        if result.status not in (ResultStatus.SIGNAL_FOUND,):
            raise ScanSetupError("only a symbol with a signal can be reviewed in the paper workflow")
        return {"symbol": result.symbol, "strategy": scan.strategy_name, "params": dict(scan.strategy_params),
                "account": scan.account, "scan_id": scan.scan_id, "decision_bar": result.decision_bar,
                "note": "A scan result is a snapshot. The paper workflow re-checks everything and needs your approval."}


def history_requirement_for(strategy_name: str, params: dict | None = None, symbol: str = "SPY") -> dict[str, Any]:
    """What the strategy DECLARES for these parameters (display only; the scan itself re-checks per symbol)."""
    from src.paper.evaluation import StrategyResolutionError, resolve_strategy
    from src.strategies.history import requirement_of
    try:
        instance, _ = resolve_strategy(strategy_name, symbol, dict(params or {}))
    except StrategyResolutionError as exc:
        return {"status": "UNRESOLVED", "detail": str(exc)}
    lk = requirement_of(instance)
    return {"status": lk.status, "detail": lk.detail, "requirement": lk.requirement.to_dict() if lk.usable else None,
            "explanation": lk.requirement.explain() if lk.usable else lk.detail}
