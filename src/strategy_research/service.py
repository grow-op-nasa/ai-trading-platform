"""The research service -- reproducible, chronologically validated strategy evaluation (Sprint 23, ADR-0061).

Flow for one (strategy, instrument, configuration set, window):

    data (MarketDataService, completed bars only, integrity-checked)  ->  immutable snapshot
      -> declared-history check  -> chronological split (development / validation / holdout)
      -> SELECTION phase: every candidate configuration is backtested on development + validation bars ONLY
      -> scorecards  -> persisted, immutable run record (holdout LOCKED)
      -> evaluate_holdout(run_id): one time only, on the configuration already selected.

Everything runs through the existing ``Backtester.run_portfolio`` (risk-sized, next-bar-open fills, slippage, fees) under
the same ``ResearchAssumptions`` for every strategy. Nothing here creates orders, intents, proposals or paper records.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import pandas as pd

from src.backtesting.engine import Backtester
from src.backtesting.models import BacktestResult
from src.calendar.nyse import NYSECalendar
from src.data.base import Interval
from src.data.exceptions import DataProviderError, DataValidationError
from src.paper.bars import split_complete
from src.snapshots import SnapshotError, SnapshotStore
from src.snapshots.meta import environment_fingerprint, snapshot_meta
from src.strategies.catalogue import metadata_for
from src.strategies.history import assess_strategy
from src.strategies.identity import strategy_version
from src.strategy_research.assumptions import STRESS_COST_MULTIPLE, ResearchAssumptions
from src.strategy_research.protocol import (
    MIN_SEGMENT_BARS,
    ProtocolError,
    ValidationProtocol,
    walk_forward_folds,
)
from src.strategy_research.governance import (
    ABORTED,
    COMPLETED as LEDGER_COMPLETED,
    canonical_spec,
    spec_hash,
    verify_spec,
    HoldoutLedger,
)
from src.strategy_research.regimes import regime_breakdown
from src.strategy_research.scorecard import Scorecard, build_scorecard, yearly_returns
from src.strategy_research.store import HoldoutAlreadyEvaluated, ResearchStore
from src.utils.hashing import dataframe_fingerprint

SCHEMA_VERSION = 1
# Version of the research ENGINE (how signals are generated/attributed). 1 = original (strategy ran through warm-up and signals were
# discarded afterwards); 2 = strategy runs over evaluable bars only (ADR-0061 correction). Runs made by another version are
# never replayed or holdout-evaluated with this one: that would mix two methodologies in one run.
# 3 = equity curve is observed at fill time, not signal time (NEXT_BAR_OPEN entries/exits no longer book the signal-bar-close ->
# next-open gap one bar early; Sprint 23 closeout, ADR-0061 amendment). Fills, trades and cash are identical to version 2.
ENGINE_VERSION = 3
MAX_CONFIGS = 5
GAP_TOLERANCE = 0.02
SUPPORTED_INTERVALS = (Interval.DAY_1.value,)
SELECTION_RULE = "highest net return on the validation segment (development and validation bars only)"

KIND_SINGLE = "SINGLE"
KIND_WALK_FORWARD = "WALK_FORWARD"
COMPLETED = "COMPLETED"
NOT_EVALUABLE = "NOT_EVALUABLE"


class DataSource(Protocol):
    def get_dataset(self, symbol: str, start: date | None = ..., end: date | None = ..., interval: Any = ...,
                    use_cache: bool | None = ...) -> Any: ...


@dataclass(frozen=True)
class ResearchRequest:
    strategy_name: str
    symbol: str
    start: date
    end: date
    configs: tuple[dict[str, Any], ...] = ({},)
    interval: str = Interval.DAY_1.value
    assumptions: ResearchAssumptions = field(default_factory=ResearchAssumptions)
    protocol: ValidationProtocol | None = None
    notes: str = ""


@dataclass(frozen=True)
class ResearchOutcome:
    status: str                       # COMPLETED | NOT_EVALUABLE
    run_id: str
    reason_code: str | None = None
    reason: str | None = None
    record: dict[str, Any] | None = None


class _NotEvaluable(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _WarmupGuard:
    """Runs a real strategy over the EVALUABLE bars only, with indicators computed on the full history.

    ``prepare`` runs on every bar so indicators are as mature as the data allows. ``generate_signals`` then receives
    only the bars from the first evaluable bar onward, so the strategy's own state starts flat there. Previously the
    strategy ran over the warm-up too and its signals were discarded afterwards; a spurious early entry (e.g. RSI = 0
    on the second bar) left the strategy believing it was long, so it could not enter again until it had exited
    (ADR-0061, correction). A strategy therefore never carries state, or a position, in from immature indicators.
    """

    def __init__(self, inner: Any, first_valid_bar: pd.Timestamp) -> None:
        self._inner = inner
        self._first = first_valid_bar

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def symbol(self) -> str:
        return self._inner.symbol

    @property
    def params(self) -> dict:
        return self._inner.params

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        return self._inner.prepare(data)

    def generate_signals(self, data: pd.DataFrame) -> list:
        return self._inner.generate_signals(data.loc[data.index >= self._first])


def params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class _RunSet:
    net: BacktestResult
    gross: BacktestResult
    stress: BacktestResult


class ResearchService:
    def __init__(self, data: DataSource, store: ResearchStore, snapshots: SnapshotStore | None = None,
                 calendar: Any | None = None, clock: Any | None = None, ledger: HoldoutLedger | None = None) -> None:
        self._data = data
        self._store = store
        # Holdout consumption is tracked by instrument + locked bar range (not run id), beside the research database.
        self._ledger = ledger or HoldoutLedger(store.path.with_name("holdout_ledger.db"))
        self._snapshots = snapshots
        self._calendar = calendar or NYSECalendar()
        self._clock = clock or (lambda: pd.Timestamp.now(tz="UTC").to_pydatetime())

    # ------------------------------------------------------------------ data
    def _load(self, request: ResearchRequest) -> tuple[Any, pd.DataFrame, list[pd.Timestamp], dict[str, Any]]:
        if request.interval not in SUPPORTED_INTERVALS:
            raise _NotEvaluable("UNSUPPORTED_TIMEFRAME",
                                f"interval {request.interval!r} is not supported by research (supported: {list(SUPPORTED_INTERVALS)}); "
                                "intraday research needs the intraday engine, which does not exist yet")
        try:
            ds = self._data.get_dataset(request.symbol, start=request.start, end=request.end, interval=Interval.DAY_1)
        except DataValidationError as exc:
            raise _NotEvaluable("DATA_INVALID", f"{type(exc).__name__}: {exc}") from exc
        except DataProviderError as exc:
            raise _NotEvaluable("PROVIDER_UNAVAILABLE", f"{type(exc).__name__}: {exc}") from exc
        except (ValueError, KeyError) as exc:
            raise _NotEvaluable("DATA_UNAVAILABLE", f"{type(exc).__name__}: {exc}") from exc
        candles, dropped = split_complete(ds.candles, self._calendar, self._clock())
        if candles.empty:
            raise _NotEvaluable("DATA_UNAVAILABLE", "no complete bars in the requested window")
        report = getattr(ds, "validation_report", None)
        gaps = int(getattr(getattr(report, "gaps", None), "count", 0) or 0)
        limits: list[str] = []
        if gaps:
            limits.append(f"{gaps} expected session(s) are missing from the data")
        if dropped:
            limits.append(f"{len(dropped)} incomplete (forming) bar(s) were excluded")
        if gaps / max(len(candles), 1) > GAP_TOLERANCE:
            raise _NotEvaluable("DATA_GAPS", f"{gaps} missing sessions in {len(candles)} bars exceeds the "
                                f"{GAP_TOLERANCE:.0%} tolerance; the data cannot support a reliable evaluation")
        prov = {
            "provider": getattr(ds, "provider", None), "symbol": request.symbol, "interval": request.interval,
            "requested_start": str(request.start), "requested_end": str(request.end),
            "first_bar": candles.index[0].isoformat(), "last_bar": candles.index[-1].isoformat(),
            "valid_bars": int(len(candles)), "dropped_incomplete": [t.isoformat() for t in dropped],
            "dataset_hash": dataframe_fingerprint(candles), "full_dataset_hash": getattr(ds, "content_hash", None),
            "gap_count": gaps, "limitations": limits,
            "environment": environment_fingerprint(),
        }
        return ds, candles, dropped, prov

    # ------------------------------------------------------------ backtests
    def _backtests(self, cls: type, symbol: str, params: dict[str, Any], candles: pd.DataFrame,
                   assumptions: ResearchAssumptions, first_valid_bar: pd.Timestamp) -> _RunSet:
        def run(multiple: float) -> BacktestResult:
            strategy = _WarmupGuard(cls(symbol=symbol, **params), first_valid_bar)
            return Backtester(assumptions.initial_cash).run_portfolio(
                {symbol: strategy}, {symbol: candles}, assumptions.to_backtest_config(multiple))
        return _RunSet(run(1.0), run(0.0), run(STRESS_COST_MULTIPLE))

    @staticmethod
    def _card(label: str, i0: int, i1: int, candles: pd.DataFrame, rs: _RunSet, cash: float) -> Scorecard:
        return build_scorecard(label=label, i0=i0, i1=i1, index=candles.index, net_trades=rs.net.trades,
                               net_equity=rs.net.equity_curve, gross_equity=rs.gross.equity_curve,
                               stress_equity=rs.stress.equity_curve, close=candles["close"], initial_cash=cash,
                               stress_multiple=STRESS_COST_MULTIPLE)

    # ------------------------------------------------------------------ run
    def run(self, request: ResearchRequest) -> ResearchOutcome:
        run_id = uuid.uuid4().hex[:16]
        cls = None
        try:
            cls = self._strategy_class(request)
            outcome = self._run(request, cls, run_id)
        except _NotEvaluable as exc:
            outcome = self._persist_not_evaluable(request, cls, run_id, exc, KIND_SINGLE)
        return outcome

    def _strategy_class(self, request: ResearchRequest) -> type:
        import src.strategies  # noqa: F401  registers the production strategies
        from src.strategies.registry import available_strategies, get_strategy_class
        if request.strategy_name not in available_strategies():
            raise _NotEvaluable("UNKNOWN_STRATEGY", f"{request.strategy_name!r} is not in the production registry")
        meta = metadata_for(request.strategy_name)
        if not meta.researchable:
            raise _NotEvaluable("NOT_RESEARCHABLE", meta.researchable_note or "not constructible from parameters")
        if request.interval not in meta.supported_intervals:
            raise _NotEvaluable("UNSUPPORTED_TIMEFRAME",
                                f"{request.strategy_name} supports {list(meta.supported_intervals)}, not {request.interval!r}")
        if not 1 <= len(request.configs) <= MAX_CONFIGS:
            raise _NotEvaluable("TOO_MANY_CONFIGS", f"between 1 and {MAX_CONFIGS} pre-specified configurations are allowed")
        return get_strategy_class(request.strategy_name)

    def _validate_configs(self, request: ResearchRequest, cls: type) -> int:
        """Instantiate every configuration; return the largest declared minimum-bars requirement."""
        need = 1
        for cfg in request.configs:
            try:
                inst = cls(symbol=request.symbol, **cfg)
            except (TypeError, ValueError) as exc:
                raise _NotEvaluable("INVALID_PARAMETERS", f"{cfg}: {type(exc).__name__}: {exc}") from exc
            lookup = assess_strategy(inst, 10 ** 9)
            if lookup.status != "DECLARED":
                raise _NotEvaluable("HISTORY_REQUIREMENT_UNKNOWN", lookup.explanation)
            need = max(need, int(lookup.required))
        return need

    def _run(self, request: ResearchRequest, cls: type, run_id: str) -> ResearchOutcome:
        need = self._validate_configs(request, cls)
        ds, candles, dropped, prov = self._load(request)
        first_valid_pos = need - 1
        eval_bars = len(candles) - first_valid_pos
        if eval_bars < 3 * MIN_SEGMENT_BARS:
            raise _NotEvaluable("INSUFFICIENT_HISTORY",
                                f"{len(candles)} complete bars leave {max(eval_bars, 0)} evaluable bars after the "
                                f"{need}-bar history requirement; at least {3 * MIN_SEGMENT_BARS} are needed")
        try:
            protocol = request.protocol or ValidationProtocol.from_fractions(eval_bars)
            if protocol.total_bars != eval_bars:
                raise ProtocolError(f"the protocol covers {protocol.total_bars} bars but {eval_bars} are evaluable")
        except ProtocolError as exc:
            raise _NotEvaluable("INVALID_PROTOCOL", str(exc)) from exc
        a = first_valid_pos
        b = a + protocol.dev_bars
        c = b + protocol.validation_bars
        first_valid_bar = candles.index[first_valid_pos]
        boundaries = protocol.boundaries(candles.index[first_valid_pos:])

        snapshot_id = self._store_snapshot(ds, candles, request)
        # SELECTION PHASE: the only candles any configuration sees end at the validation segment's last bar.
        sel_candles = candles.iloc[:c]
        attempts: list[dict[str, Any]] = []
        sel_runs: list[_RunSet] = []
        for i, cfg in enumerate(request.configs):
            rs = self._backtests(cls, request.symbol, cfg, sel_candles, request.assumptions, first_valid_bar)
            sel_runs.append(rs)
            cards = {"development": self._card("development", a, b, sel_candles, rs, request.assumptions.initial_cash),
                     "validation": self._card("validation", b, c, sel_candles, rs, request.assumptions.initial_cash)}
            attempts.append({"index": i, "params": cfg, "params_hash": params_hash(cfg),
                             "scorecards": {k: v.to_dict() for k, v in cards.items()}})
        sel_index, sel_reason = self._select([Scorecard.from_dict(x["scorecards"]["validation"]) for x in attempts])
        chosen = sel_runs[sel_index]
        chosen_cfg = request.configs[sel_index]
        yearly = yearly_returns(sel_candles.index, chosen.net.equity_curve, sel_candles["close"], a, c)
        regimes = regime_breakdown(sel_candles, chosen.net.equity_curve, a, c)
        instance = cls(symbol=request.symbol, **chosen_cfg)
        requirement = instance.history_requirement().to_dict()
        record = {
            "schema": SCHEMA_VERSION, "run_id": run_id, "kind": KIND_SINGLE, "status": COMPLETED,
            "request": {"strategy": request.strategy_name, "symbol": request.symbol, "interval": request.interval,
                        "start": str(request.start), "end": str(request.end), "configs": list(request.configs),
                        "notes": request.notes, "protocol": protocol.to_dict()},
            "engine": {"version": ENGINE_VERSION},
            "strategy": {"id": request.strategy_name, "version": strategy_version(cls),
                         "family": metadata_for(request.strategy_name).family.value},
            "assumptions": request.assumptions.to_dict(), "cost_note": request.assumptions.describe_costs(),
            "provenance": {**prov, "snapshot_id": snapshot_id},
            "history": {"requirement": requirement, "max_requirement_bars": need,
                        "first_valid_bar": first_valid_bar.isoformat(), "evaluable_bars": eval_bars},
            "boundaries": boundaries, "attempts": attempts,
            "selection": {"rule": SELECTION_RULE, "selected_index": sel_index, "reason": sel_reason,
                          "configurations_attempted": len(request.configs)},
            "selected": {"params": chosen_cfg, "params_hash": params_hash(chosen_cfg), "yearly": yearly, "regimes": regimes},
            "holdout": {"status": "LOCKED", "note": "evaluated once, on the already-selected configuration"},
            "limitations": self._limitations(request, prov),
        }
        # Freeze the research specification BEFORE any holdout bar is evaluated (hash re-verified at holdout time).
        spec = canonical_spec(
            strategy=record["strategy"], params=chosen_cfg, request=record["request"], assumptions=record["assumptions"],
            engine_version=ENGINE_VERSION, provenance=record["provenance"], boundaries=boundaries,
            holdout_hash=dataframe_fingerprint(candles.iloc[c:]), selection=record["selection"], configs=list(request.configs))
        record["spec"], record["spec_hash"] = spec, spec_hash(spec)
        self._store.add_run(run_id, KIND_SINGLE, request.strategy_name, record["strategy"]["version"], request.symbol,
                            request.interval, COMPLETED, record)
        return ResearchOutcome(COMPLETED, run_id, record=record)

    @staticmethod
    def _select(validation_cards: list[Scorecard]) -> tuple[int, str]:
        if len(validation_cards) == 1:
            return 0, "a single pre-specified configuration: nothing was selected between alternatives"
        best, best_val = 0, None
        for i, card in enumerate(validation_cards):
            if card.net_return.is_defined and (best_val is None or card.net_return.value > best_val):
                best, best_val = i, card.net_return.value
        if best_val is None:
            return 0, "no configuration had a defined validation return; the first was kept, with no basis to prefer another"
        return best, f"configuration {best} had the highest validation net return ({best_val:.2%}) of {len(validation_cards)} attempted"

    @staticmethod
    def _limitations(request: ResearchRequest, prov: dict[str, Any]) -> list[str]:
        out = [
            "Selection bias: every configuration attempted is recorded; the more configurations tried, the less a good "
            "validation result means.",
            "Fills use the next bar's open with simulated costs; real execution can be worse.",
            "Daily bars only: this is not intraday research.",
            "Data is split chronologically; a single holdout is one sample of the future, not a guarantee.",
        ]
        if request.symbol.upper() not in ("SPY", "QQQ", "IWM", "DIA"):
            out.append("Universe bias: a currently-listed instrument chosen today is not a point-in-time universe; "
                       "results do not describe unbiased historical stock selection.")
        out.extend(prov.get("limitations", []))
        return out

    def _store_snapshot(self, ds: Any, candles: pd.DataFrame, request: ResearchRequest) -> str | None:
        if self._snapshots is None:
            return None
        meta = snapshot_meta(ds, request.symbol, request.interval)
        meta["content_hash"] = dataframe_fingerprint(candles)   # the stored (completed-bars) frame, not the raw dataset
        try:
            return self._snapshots.put(candles, meta).snapshot_id
        except SnapshotError as exc:
            raise _NotEvaluable("SNAPSHOT_FAILED", f"the dataset could not be stored and verified: {exc}") from exc

    def _persist_not_evaluable(self, request: ResearchRequest, cls: type | None, run_id: str, exc: _NotEvaluable,
                               kind: str) -> ResearchOutcome:
        version = None
        if cls is not None:
            try:
                version = strategy_version(cls)
            except TypeError:
                version = None
        record = {
            "schema": SCHEMA_VERSION, "run_id": run_id, "kind": kind, "status": NOT_EVALUABLE,
            "reason_code": exc.code, "reason": exc.reason,
            "request": {"strategy": request.strategy_name, "symbol": request.symbol, "interval": request.interval,
                        "start": str(request.start), "end": str(request.end), "configs": list(request.configs),
                        "notes": request.notes},
            "assumptions": request.assumptions.to_dict(),
        }
        self._store.add_run(run_id, kind, request.strategy_name, version, request.symbol, request.interval,
                            NOT_EVALUABLE, record)
        return ResearchOutcome(NOT_EVALUABLE, run_id, exc.code, exc.reason, record)

    # -------------------------------------------------------------- holdout
    def evaluate_holdout(self, run_id: str) -> dict[str, Any]:
        """Evaluate the locked holdout of a completed run, once, using the configuration already selected."""
        run = self._store.get_run(run_id)
        if run is None or run["kind"] != KIND_SINGLE:
            raise KeyError(f"no single-configuration research run {run_id!r}")
        rec = run["payload"]
        if rec["status"] != COMPLETED:
            raise ValueError("a run that was not evaluable has no holdout to evaluate")
        if self._store.get_holdout(run_id) is not None:
            raise HoldoutAlreadyEvaluated(f"the holdout of run {run_id} has already been evaluated")
        if rec.get("engine", {}).get("version", 1) != ENGINE_VERSION:
            raise ValueError("this run was created by an earlier research engine version; its selection phase used a different "
                             "methodology, so its holdout cannot be evaluated with the current engine. Create a new run instead")
        import src.strategies  # noqa: F401
        from src.strategies.registry import get_strategy_class
        cls = get_strategy_class(rec["strategy"]["id"])
        if strategy_version(cls) != rec["strategy"]["version"]:
            raise ValueError("the strategy implementation has changed since this run was created; the holdout cannot "
                             "be evaluated against it. Create a new run instead")
        if self._snapshots is None or not rec["provenance"].get("snapshot_id"):
            raise ValueError("this run has no stored data snapshot, so its holdout cannot be evaluated reproducibly")
        verify_spec(rec)                                    # raises SpecMismatch before anything touches the holdout bars
        snap = self._snapshots.load(rec["provenance"]["snapshot_id"])
        candles = snap.candles
        if dataframe_fingerprint(candles.iloc[int(rec["boundaries"]["holdout"]["start_pos"]) + int(rec["history"]["max_requirement_bars"]) - 1:]) \
                != rec["spec"]["data"]["holdout_slice_hash"]:
            raise ValueError("the stored data snapshot no longer matches the holdout slice frozen in the specification")
        hb = rec["boundaries"]["holdout"]
        # Consumption is keyed by instrument + locked bar range, not run id: another run over the same bars cannot re-open it.
        reservation = self._ledger.reserve(
            run_id=run_id, symbol=rec["request"]["symbol"], interval=rec["request"]["interval"], first_bar=hb["first_bar"],
            last_bar=hb["last_bar"], holdout_hash=rec["spec"]["data"]["holdout_slice_hash"], spec_hash_=rec["spec_hash"])
        assumptions = ResearchAssumptions.from_dict(rec["assumptions"])
        first_valid_pos = int(rec["history"]["max_requirement_bars"]) - 1
        first_valid_bar = candles.index[first_valid_pos]
        proto = ValidationProtocol.from_dict(rec["request"]["protocol"])
        a = first_valid_pos
        c = a + proto.dev_bars + proto.validation_bars
        cfg = rec["selected"]["params"]
        rs = self._backtests(cls, rec["request"]["symbol"], cfg, candles, assumptions, first_valid_bar)
        card = self._card("holdout", c, len(candles), candles, rs, assumptions.initial_cash)
        selection_val = Scorecard.from_dict(rec["attempts"][rec["selection"]["selected_index"]]["scorecards"]["validation"])
        dev = Scorecard.from_dict(rec["attempts"][rec["selection"]["selected_index"]]["scorecards"]["development"])
        payload = {
            "run_id": run_id, "scorecard": card.to_dict(), "configuration": cfg, "params_hash": params_hash(cfg),
            "yearly": yearly_returns(candles.index, rs.net.equity_curve, candles["close"], c, len(candles)),
            "regimes": regime_breakdown(candles, rs.net.equity_curve, c, len(candles)),
            "assessment": assess_out_of_sample(dev, selection_val, card),
            "data": {"snapshot_id": rec["provenance"]["snapshot_id"], "dataset_hash": rec["provenance"]["dataset_hash"]},
        }
        try:
            self._store.add_holdout(run_id, payload)
        except HoldoutAlreadyEvaluated:
            raise                                   # the figures exist: the holdout stays consumed
        except Exception as exc:
            # Computed but never persisted, returned or shown: discarding it is safe and a retry of the identical frozen
            # spec reproduces the same numbers, so this (and only this) failure releases the reservation.
            self._ledger.finish(reservation, ABORTED, f"{type(exc).__name__}: {exc}")
            raise
        self._ledger.finish(reservation, LEDGER_COMPLETED)
        return payload

    # ---------------------------------------------------------- walk-forward
    def walk_forward(self, request: ResearchRequest, train_bars: int, test_bars: int) -> ResearchOutcome:
        run_id = uuid.uuid4().hex[:16]
        cls = None
        try:
            cls = self._strategy_class(request)
            return self._walk_forward(request, cls, run_id, train_bars, test_bars)
        except _NotEvaluable as exc:
            return self._persist_not_evaluable(request, cls, run_id, exc, KIND_WALK_FORWARD)

    def _walk_forward(self, request: ResearchRequest, cls: type, run_id: str, train_bars: int, test_bars: int) -> ResearchOutcome:
        need = self._validate_configs(request, cls)
        ds, candles, dropped, prov = self._load(request)
        first_valid_pos = need - 1
        eval_total = len(candles) - first_valid_pos
        try:
            folds = walk_forward_folds(eval_total, train_bars, test_bars)
        except ProtocolError as exc:
            raise _NotEvaluable("INVALID_PROTOCOL", str(exc)) from exc
        first_valid_bar = candles.index[first_valid_pos]
        snapshot_id = self._store_snapshot(ds, candles, request)
        runs = [self._backtests(cls, request.symbol, cfg, candles, request.assumptions, first_valid_bar)
                for cfg in request.configs]
        cash = request.assumptions.initial_cash
        fold_rows, chained, test_trades = [], 1.0, 0
        for k, f in enumerate(folds):
            off = first_valid_pos
            train_cards = [self._card("train", off + f.train_start, off + f.train_end, candles, rs, cash) for rs in runs]
            sel, why = self._select(train_cards)
            test_card = self._card("test", off + f.test_start, off + f.test_end, candles, runs[sel], cash)
            if test_card.net_return.is_defined:
                chained *= 1.0 + test_card.net_return.value
            test_trades += test_card.closed_trades
            fold_rows.append({"fold": k, "train": [off + f.train_start, off + f.train_end], "test": [off + f.test_start, off + f.test_end],
                              "train_first_bar": candles.index[off + f.train_start].isoformat(),
                              "train_last_bar": candles.index[off + f.train_end - 1].isoformat(),
                              "test_first_bar": candles.index[off + f.test_start].isoformat(),
                              "test_last_bar": candles.index[off + f.test_end - 1].isoformat(),
                              "selected_index": sel, "selected_params": request.configs[sel], "selection_reason": why,
                              "test_scorecard": test_card.to_dict()})
        record = {
            "schema": SCHEMA_VERSION, "run_id": run_id, "kind": KIND_WALK_FORWARD, "status": COMPLETED,
            "request": {"strategy": request.strategy_name, "symbol": request.symbol, "interval": request.interval,
                        "start": str(request.start), "end": str(request.end), "configs": list(request.configs),
                        "train_bars": train_bars, "test_bars": test_bars, "notes": request.notes},
            "engine": {"version": ENGINE_VERSION},
            "strategy": {"id": request.strategy_name, "version": strategy_version(cls),
                         "family": metadata_for(request.strategy_name).family.value},
            "assumptions": request.assumptions.to_dict(), "cost_note": request.assumptions.describe_costs(),
            "provenance": {**prov, "snapshot_id": snapshot_id},
            "history": {"max_requirement_bars": need, "first_valid_bar": first_valid_bar.isoformat(), "evaluable_bars": eval_total},
            "folds": fold_rows,
            "aggregate": {"folds": len(folds), "out_of_sample_net_return_chained": chained - 1.0,
                          "out_of_sample_closed_trades": test_trades, "configurations_attempted": len(request.configs)},
            "method": ("Rolling walk-forward: for each fold the configuration is chosen using only the training window's net "
                       "return, then evaluated on the following unseen test window. Configurations are the pre-specified "
                       "list; nothing is optimised or re-fitted. With a single configuration this is a rolling "
                       "out-of-sample evaluation of that fixed configuration."),
            "limitations": self._limitations(request, prov),
        }
        self._store.add_run(run_id, KIND_WALK_FORWARD, request.strategy_name, record["strategy"]["version"], request.symbol,
                            request.interval, COMPLETED, record)
        return ResearchOutcome(COMPLETED, run_id, record=record)

    # ------------------------------------------------------------ reproduce
    def reproduce(self, run_id: str) -> dict[str, Any]:
        """Replay a stored single run from its own snapshot: data, strategy, backtest and metric layers, reported separately."""
        run = self._store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        rec = run["payload"]
        out: dict[str, Any] = {"run_id": run_id, "data": "NOT_REPLAYABLE", "strategy": "NOT_REPLAYABLE", "backtest": "NOT_REPLAYABLE", "detail": []}
        if rec["status"] != COMPLETED or rec["kind"] != KIND_SINGLE:
            out["detail"].append("only completed single-configuration runs can be replayed")
            return out
        snap_id = rec["provenance"].get("snapshot_id")
        if self._snapshots is None or not snap_id:
            out["detail"].append("no stored data snapshot: the original data cannot be reconstructed and today's data will not be substituted")
            return out
        try:
            snap = self._snapshots.load(snap_id)
        except SnapshotError as exc:
            out["detail"].append(f"snapshot unavailable or damaged: {exc}")
            return out
        if dataframe_fingerprint(snap.candles) != rec["provenance"]["dataset_hash"]:
            out["data"] = "DIFFERENT"
            out["detail"].append("the snapshot content does not match the recorded dataset hash")
            return out
        out["data"] = "VERIFIED"
        import src.strategies  # noqa: F401
        from src.strategies.registry import get_strategy_class
        cls = get_strategy_class(rec["strategy"]["id"])
        if strategy_version(cls) != rec["strategy"]["version"]:
            out["strategy"] = "STRATEGY_CHANGED"
            out["detail"].append("the strategy source differs from the one that produced this run")
            return out
        out["strategy"] = "SAME_SOURCE"
        stored_engine = rec.get("engine", {}).get("version", 1)
        if stored_engine != ENGINE_VERSION:
            out["backtest"] = "ENGINE_CHANGED"
            out["detail"].append(f"this run was produced by research engine v{stored_engine}; the current engine is v{ENGINE_VERSION}. "
                                 "Its numbers are not comparable with a replay and should not be relied on. Create a new run")
            return out
        assumptions = ResearchAssumptions.from_dict(rec["assumptions"])
        candles = snap.candles
        proto = ValidationProtocol.from_dict(rec["request"]["protocol"])
        a = int(rec["history"]["max_requirement_bars"]) - 1
        b, c = a + proto.dev_bars, a + proto.dev_bars + proto.validation_bars
        idx = rec["selection"]["selected_index"]
        sel = candles.iloc[:c]
        rs = self._backtests(cls, rec["request"]["symbol"], rec["request"]["configs"][idx], sel, assumptions, candles.index[a])
        stored = rec["attempts"][idx]["scorecards"]
        again = {"development": self._card("development", a, b, sel, rs, assumptions.initial_cash).to_dict(),
                 "validation": self._card("validation", b, c, sel, rs, assumptions.initial_cash).to_dict()}
        same = stored == again
        out["backtest"] = "REPRODUCED" if same else "DIFFERENT"
        out["detail"].append("development and validation scorecards were recomputed from the stored data and "
                             + ("match exactly" if same else "DIFFER from the stored values"))
        return out


def assess_out_of_sample(dev: Scorecard, validation: Scorecard, holdout: Scorecard) -> list[str]:
    """Plain statements derived only from the three scorecards' measured values."""
    notes: list[str] = []
    if holdout.net_return.is_defined and not holdout.net_return.value > 0 and dev.net_return.is_defined and dev.net_return.value > 0:
        notes.append("Positive in development but not positive out of sample: the in-sample result did not hold up.")
    if holdout.net_return.is_defined and holdout.net_return.value > 0 and validation.net_return.is_defined and validation.net_return.value <= 0:
        notes.append("Holdout is positive although validation was not: treat as inconclusive, not as confirmation.")
    if holdout.sample in ("NONE", "VERY_SMALL", "SMALL"):
        notes.append(f"Holdout has only {holdout.closed_trades} closed trades: insufficient for a reliable conclusion.")
    if (holdout.expectancy.is_defined and validation.expectancy.is_defined and validation.expectancy.value > 0
            and holdout.expectancy.value <= 0):
        notes.append("Expectancy turned from positive in validation to non-positive in the holdout.")
    if holdout.max_drawdown.is_defined and validation.max_drawdown.is_defined and holdout.max_drawdown.value < 2 * validation.max_drawdown.value < 0:
        notes.append("Holdout drawdown is more than twice the validation drawdown.")
    if not notes:
        notes.append("No out-of-sample degradation flagged by these checks (this is not a statement of profitability).")
    return notes


# ---------------------------------------------------------------- research state
RESEARCH_STATES = ("EXPERIMENTAL", "VALIDATION_PENDING", "RESEARCH_CANDIDATE", "OUT_OF_SAMPLE_EVALUATED", "REJECTED",
                   "ELIGIBLE_FOR_PAPER_EVALUATION")


def research_state(run: dict[str, Any] | None, holdout: dict[str, Any] | None) -> tuple[str, str]:
    """The research maturity of one run, derived only from its stored scorecards. Never implies promotion."""
    from src.strategy_research.scorecard import MIN_RELIABLE_TRADES
    if run is None or run["payload"]["status"] != COMPLETED or run["kind"] != KIND_SINGLE:
        return "EXPERIMENTAL", "no completed validation run exists for this configuration"
    rec = run["payload"]
    if rec.get("engine", {}).get("version", 1) != ENGINE_VERSION:
        return "EXPERIMENTAL", "this run was produced by an earlier research engine version and is not counted"
    sel = rec["attempts"][rec["selection"]["selected_index"]]["scorecards"]
    val = Scorecard.from_dict(sel["validation"])
    if holdout is None:
        if val.closed_trades + val.open_trades_at_end == 0:
            return "VALIDATION_PENDING", "the strategy made no trades in the validation segment, so there is no evidence either way"
        if not val.net_return.is_defined:
            return "VALIDATION_PENDING", "the validation segment produced no defined return; more evidence is needed"
        if val.net_return.value <= 0:
            return "REJECTED", "net return on the validation segment was not positive"
        return "RESEARCH_CANDIDATE", "passed the validation screen; the holdout has not been evaluated"
    card = Scorecard.from_dict(holdout["payload"]["scorecard"])
    if card.closed_trades + card.open_trades_at_end == 0:
        return "OUT_OF_SAMPLE_EVALUATED", "the holdout was evaluated but the strategy made no trades in it: nothing was demonstrated"
    if not card.net_return.is_defined or card.net_return.value <= 0:
        return "REJECTED", "net return on the untouched holdout was not positive"
    ok = (card.closed_trades >= MIN_RELIABLE_TRADES and card.net_return_at_stress_costs.is_defined
          and card.net_return_at_stress_costs.value > 0 and card.expectancy.is_defined and card.expectancy.value > 0)
    if ok:
        return "ELIGIBLE_FOR_PAPER_EVALUATION", ("positive holdout with an adequate sample that survives stressed costs; "
                                                 "a human decision is still required and nothing is promoted")
    return "OUT_OF_SAMPLE_EVALUATED", "holdout evaluated and positive, but the sample or cost robustness is not sufficient to go further"
