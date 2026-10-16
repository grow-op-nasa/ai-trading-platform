"""Paper-trading runtime (Sprint 18, ADR-0053): the production orchestration path, network-free.

Data comes from `SyntheticCandleProvider` (optionally wrapped in `FaultInjectingProvider`) through the real
`MarketDataService`; the strategy is a scripted, *registered* test strategy; risk, execution cost model, `PaperBroker`
accounting, the experiment registry portfolio and the monitoring service are the real components. Fixtures are
synthetic and disposable. Nothing here contacts a network, a live broker, an LLM or an SMTP server, and a FILLED
status means "filled by the paper broker", never a real-market fill.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import sqlite3
import subprocess
import sys
import types
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from src.data.base import Interval
from src.data.fault_injection import Fault, FaultInjectingProvider, FaultKind, SyntheticCandleProvider
from src.data.service import MarketDataService
from src.experiments.registry import ExperimentRegistry
from src.experiments.spec import ExperimentSpec
from src.paper.config import PaperConfigError, PaperRunConfig
from src.paper.faults import BrokerUncertain, Faults, SimulatedCrash
from src.paper.models import CheckStatus, CycleStatus, IntentState, ReconStatus
from src.paper.runtime import PaperLockedError, PaperSetupError, PaperTradingRuntime
from src.paper.store import PaperStore
from src.portfolio.models import Portfolio
from src.portfolio.position import Position, PositionSide
from src.signals.models import Signal, SignalDirection
from src.strategies.identity import strategy_version
from src.strategies.registry import _unregister_strategy, register_strategy

REPO = Path(__file__).resolve().parent.parent
WED = date(2026, 10, 7)
AFTER_CLOSE = datetime(2026, 10, 7, 22, 0, tzinfo=timezone.utc)      # NYSE closes 20:00Z in October (EDT)
NEXT_DAY_AFTER_CLOSE = datetime(2026, 10, 8, 22, 0, tzinfo=timezone.utc)
STRAT = "paper_scripted_test"


@register_strategy(STRAT)
class ScriptedStrategy:
    """Registered test strategy: emits the direction scripted for a bar's date (class-level, set by each test)."""

    SCRIPT: dict[date, str] = {}
    SEEN_MAX: list = []
    RAISE = False

    def __init__(self, symbol: str, **params) -> None:
        self.symbol = symbol

    @property
    def name(self) -> str:
        return STRAT

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        if ScriptedStrategy.RAISE:
            raise RuntimeError("scripted strategy failure")
        ScriptedStrategy.SEEN_MAX.append(pd.Timestamp(data.index.max()))
        return data.copy()

    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        out = []
        for ts in data.index:
            direction = ScriptedStrategy.SCRIPT.get(pd.Timestamp(ts).date())
            if direction:
                out.append(Signal(timestamp=pd.Timestamp(ts), symbol=self.symbol,
                                  direction=SignalDirection(direction), confidence=0.9))
        return out


@pytest.fixture(autouse=True)
def _reset_script():
    ScriptedStrategy.SCRIPT = {}
    ScriptedStrategy.SEEN_MAX = []
    ScriptedStrategy.RAISE = False
    yield


def base_config(**over) -> dict:
    cfg = {"execution_mode": "paper", "experiment_id": 1, "strategy_name": STRAT, "symbol": "QQQ", "interval": "1d",
           "lookback_days": 400,
           "risk": {"allocation_per_trade_pct": 0.10, "max_portfolio_exposure_pct": 0.50, "risk_pct_per_trade": 0.01},
           "stop": {"type": "atr", "period": 14, "multiple": 2.0},
           "execution": {"timing": "SIGNAL_BAR_CLOSE", "slippage_bps": 0.0, "fee_bps": 0.0, "fixed_fee": 0.0}}
    cfg.update(over)
    return cfg


class World:
    def __init__(self, tmp: Path, *, last_session: date = WED, now: datetime = AFTER_CLOSE, spec_exposure: float = 0.5,
                 initialize: bool = True, config_over: dict | None = None, equity_points: int = 0) -> None:
        self.tmp = tmp
        self.reg_path = tmp / "experiments.db"
        self.store_path = tmp / "paper" / "paper_runtime.db"
        self.registry = ExperimentRegistry(self.reg_path)
        self.exp = self._seed(spec_exposure, equity_points)
        self.provider = FaultInjectingProvider(SyntheticCandleProvider(last_session), [None])
        self.now = [now]
        self.faults = Faults()
        self.monitor_calls: list[int] = []
        self.monitor_error: Exception | None = None
        self.cfg_dict = base_config(experiment_id=self.exp, **(config_over or {}))
        if initialize:
            self.runtime().initialize_account("synthetic test fixture")

    def _seed(self, spec_exposure: float, equity_points: int) -> int:
        from src.strategies.registry import get_strategy_class
        exp = self.registry.log_experiment(strategy_name=STRAT, changed={}, metrics_before={}, metrics_after={},
                                           decision="KEEP", notes="SYNTHETIC paper runtime test fixture")
        self.registry.save_spec(exp, ExperimentSpec(
            strategy_name=STRAT, strategy_version=strategy_version(get_strategy_class(STRAT)), strategy_params={},
            symbol="QQQ", interval=Interval.DAY_1, dataset_start=pd.Timestamp("2026-01-01", tz="UTC"),
            dataset_end=pd.Timestamp("2026-09-30", tz="UTC"), dataset_source="synthetic-test",
            dataset_fingerprint="synthetic-test", risk_config={"max_portfolio_exposure_pct": spec_exposure}))
        self.registry.save_portfolio(exp, Portfolio.reconstruct(cash=10_000.0, positions=[]))
        if equity_points:
            idx = pd.date_range("2026-09-01", periods=equity_points, freq="D", tz="UTC")
            self.registry.save_equity_curve(exp, pd.Series([10_000.0 + i for i in range(equity_points)], index=idx))
        return exp

    def config(self) -> PaperRunConfig:
        return PaperRunConfig.from_dict(self.cfg_dict)

    def monitor(self):
        world = self

        class _Svc:
            def run_once(self_inner):
                if world.monitor_error is not None:
                    raise world.monitor_error
                world.monitor_calls.append(1)
                return types.SimpleNamespace(run_id=f"mon-{len(world.monitor_calls)}",
                                             status=types.SimpleNamespace(value="HEALTHY"), persisted=True)
        return _Svc()

    def runtime(self, *, with_monitor: bool = True, **kw) -> PaperTradingRuntime:
        md = MarketDataService(provider=self.provider, cache_dir=self.tmp / "cache", use_cache=False)
        kw.setdefault("faults", self.faults)
        return PaperTradingRuntime(
            self.config(), registry_path=self.reg_path, store_path=self.store_path, market_data=md,
            monitor_factory=self.monitor if with_monitor else None, clock=lambda: self.now[0],
            allowed_strategy_prefixes=("src.strategies", __name__), **kw)

    def store(self) -> PaperStore:
        return PaperStore.open_existing(self.store_path)

    def portfolio(self) -> Portfolio:
        return ExperimentRegistry(self.reg_path).get_portfolio(self.exp)

    def script(self, day: date, direction: str) -> None:
        ScriptedStrategy.SCRIPT[day] = direction

    def advance(self, last_session: date, now: datetime) -> None:
        self.provider = FaultInjectingProvider(SyntheticCandleProvider(last_session), [None])
        self.now[0] = now


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def fingerprint(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            h.update(repr(conn.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall()).encode())
        return h.hexdigest()
    finally:
        conn.close()


def state_fp(w: World) -> tuple[str, str]:
    return fingerprint(w.reg_path), fingerprint(w.store_path)


# =====================================================================================
# L1. configuration, construction, preflight
# =====================================================================================
def test_valid_configuration_has_a_stable_identity_that_changes_with_material_settings():
    a = PaperRunConfig.from_dict(base_config())
    assert a.identity_hash() == PaperRunConfig.from_dict(base_config()).identity_hash()
    assert a.identity_hash() != PaperRunConfig.from_dict(base_config(strategy_params={"x": 1})).identity_hash()
    b = base_config(); b["execution"] = {**b["execution"], "fee_bps": 5.0}
    assert a.identity_hash() != PaperRunConfig.from_dict(b).identity_hash()


@pytest.mark.parametrize("patch,needle", [
    ({"execution_mode": "live"}, "paper"),
    ({"execution_mode": "paper-or-live"}, "paper"),
    ({"experiment_id": 0}, "experiment_id"),
    ({"experiment_id": True}, "experiment_id"),
    ({"strategy_name": ""}, "strategy_name"),
    ({"interval": "1m"}, "not supported"),
    ({"interval": "7d"}, "unknown interval"),
    ({"lookback_days": 5}, "lookback_days"),
    ({"broker": "alpaca"}, "unknown configuration key"),
    ({"stop": {"type": "fixed", "period": 14, "multiple": 2.0}}, "stop.type"),
    ({"stop": {"type": "atr", "period": 14}}, "explicitly set"),
    ({"risk": {"allocation_per_trade_pct": 0.1}}, "explicitly set"),
    ({"risk": {"allocation_per_trade_pct": 5.0, "max_portfolio_exposure_pct": 0.5, "risk_pct_per_trade": 0.01}}, "invalid"),
    ({"execution": {"timing": "NEXT_BAR_OPEN", "slippage_bps": 0, "fee_bps": 0, "fixed_fee": 0}}, "SIGNAL_BAR_CLOSE"),
    ({"execution": {"timing": "SIGNAL_BAR_CLOSE", "slippage_bps": -1, "fee_bps": 0, "fixed_fee": 0}}, "invalid"),
])
def test_invalid_or_ambiguous_configuration_is_rejected_with_a_clear_reason(patch, needle):
    with pytest.raises(PaperConfigError, match=needle):
        PaperRunConfig.from_dict(base_config(**patch))


def test_missing_required_sections_are_rejected_not_defaulted():
    for key in ("execution_mode", "experiment_id", "strategy_name", "symbol", "risk", "stop", "execution"):
        raw = base_config(); raw.pop(key)
        with pytest.raises(PaperConfigError, match="missing required"):
            PaperRunConfig.from_dict(raw)


def test_runtime_constructor_has_no_way_to_inject_a_broker_or_select_an_adapter():
    params = set(inspect.signature(PaperTradingRuntime.__init__).parameters)
    assert not params & {"broker", "adapter", "broker_factory", "execution_adapter", "broker_name", "live"}
    assert not hasattr(PaperTradingRuntime, "set_broker")


def test_preflight_passes_on_a_healthy_world_and_is_read_only(world):
    before = state_fp(world)
    rep = world.runtime().preflight()
    names = {c.name: c.status for c in rep.checks}
    assert rep.ready and rep.overall is CheckStatus.PASS, [c.to_dict() for c in rep.checks]
    for required in ("configuration", "paper_only_execution", "paper_broker", "registry", "portfolio", "strategy",
                     "paper_database", "paper_account", "unresolved_intents", "reconciliation", "market_data",
                     "alerting", "monitoring", "duplicate_run_protection", "persistence_writable"):
        assert required in names, required
    assert state_fp(world) == before                      # no mutation of registry or paper database
    assert world.store().list_cycles(world.exp) == []


def test_preflight_reports_missing_prerequisites_instead_of_passing(tmp_path):
    w = World(tmp_path, initialize=False)
    rep = w.runtime().preflight()
    by = {c.name: c for c in rep.checks}
    assert by["paper_database"].status is CheckStatus.FAIL and not rep.ready and rep.overall is CheckStatus.FAIL
    assert not w.store_path.exists()                      # preflight never creates the paper database
    w.reg_path.unlink()
    by = {c.name: c for c in w.runtime().preflight().checks}
    assert by["registry"].status is CheckStatus.FAIL


def test_preflight_fails_for_unknown_strategy_missing_portfolio_and_data_failure(world):
    cfg = dict(world.cfg_dict, strategy_name="no_such_strategy")
    rt = PaperTradingRuntime(PaperRunConfig.from_dict(cfg), registry_path=world.reg_path, store_path=world.store_path,
                             market_data=MarketDataService(provider=world.provider, cache_dir=world.tmp / "c", use_cache=False),
                             clock=lambda: world.now[0])
    assert {c.name: c.status for c in rt.preflight().checks}["strategy"] is CheckStatus.FAIL
    cfg = dict(world.cfg_dict, experiment_id=999)
    rt = PaperTradingRuntime(PaperRunConfig.from_dict(cfg), registry_path=world.reg_path, store_path=world.store_path,
                             market_data=MarketDataService(provider=world.provider, cache_dir=world.tmp / "c", use_cache=False),
                             clock=lambda: world.now[0], allowed_strategy_prefixes=("src.strategies", __name__))
    assert {c.name: c.status for c in rt.preflight().checks}["portfolio"] is CheckStatus.FAIL
    world.provider.set_script([Fault(FaultKind.TIMEOUT)])
    assert {c.name: c.status for c in world.runtime().preflight().checks}["market_data"] is CheckStatus.FAIL


def test_a_strategy_from_a_non_production_module_is_never_resolved(world):
    class Candidate(ScriptedStrategy):
        pass
    Candidate.__module__ = "src.ai.agents.strategy_dev.candidate_workspace"
    register_strategy("paper_candidate_only")(Candidate)
    try:
        cfg = dict(world.cfg_dict, strategy_name="paper_candidate_only")
        rt = PaperTradingRuntime(PaperRunConfig.from_dict(cfg), registry_path=world.reg_path, store_path=world.store_path,
                                 market_data=MarketDataService(provider=world.provider, cache_dir=world.tmp / "c", use_cache=False),
                                 clock=lambda: world.now[0])      # default prefixes: src.strategies only
        with pytest.raises(PaperSetupError, match="not an approved production strategy module"):
            rt.resolve_strategy()
    finally:
        _unregister_strategy("paper_candidate_only")


def test_run_without_initialised_account_fails_and_creates_nothing(tmp_path):
    w = World(tmp_path, initialize=False)
    with pytest.raises(PaperSetupError, match="not found"):
        w.runtime().run_cycle()
    assert not w.store_path.exists()
    PaperStore.create(w.store_path)
    with pytest.raises(PaperSetupError, match="no paper account"):
        w.runtime().run_cycle()


def test_missing_portfolio_fails_clearly_without_creating_anything(world):
    cfg = dict(world.cfg_dict, experiment_id=424242)
    rt = PaperTradingRuntime(PaperRunConfig.from_dict(cfg), registry_path=world.reg_path, store_path=world.store_path,
                             market_data=MarketDataService(provider=world.provider, cache_dir=world.tmp / "c", use_cache=False),
                             clock=lambda: world.now[0], allowed_strategy_prefixes=("src.strategies", __name__))
    with pytest.raises(PaperSetupError):
        rt.run_cycle()


# =====================================================================================
# L2. market data, closed bars, strategy
# =====================================================================================
def test_no_signal_is_a_persisted_no_action_and_a_repeat_is_a_replayed_noop(world):
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.NO_ACTION and rec.reason_code == "NO_SIGNAL"
    assert rec.decision_bar.startswith("2026-10-07") and rec.dataset["cache_used"] is False
    before = state_fp(world)
    again = world.runtime().run_cycle()
    assert again.replayed and again.cycle_id == rec.cycle_id and state_fp(world) == before
    assert world.store().list_intents(world.exp) == [] and world.store().list_fills(world.exp) == []


def test_incomplete_bar_is_dropped_and_the_strategy_never_sees_the_future(tmp_path):
    w = World(tmp_path, now=datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc))   # session still open
    w.script(date(2026, 10, 6), "LONG")
    rec = w.runtime().run_cycle()
    assert rec.decision_bar.startswith("2026-10-06")                 # yesterday's complete bar
    assert any(t.startswith("2026-10-07") for t in rec.dataset["dropped_incomplete_bars"])
    assert max(ScriptedStrategy.SEEN_MAX) == pd.Timestamp("2026-10-06", tz="UTC")
    assert rec.status is CycleStatus.ORDER_FILLED


def test_closed_market_weekend_is_not_a_stale_feed(tmp_path):
    w = World(tmp_path, last_session=date(2026, 10, 9), now=datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc))
    rec = w.runtime().run_cycle()                                    # Saturday: Friday's bar is the decision bar
    assert rec.status is CycleStatus.NO_ACTION and rec.decision_bar.startswith("2026-10-09")


def test_stale_data_blocks_while_a_just_closed_session_within_grace_is_only_awaiting(tmp_path):
    stale = World(tmp_path / "a", last_session=date(2026, 10, 1))
    rec = stale.runtime().run_cycle()
    assert rec.status is CycleStatus.BLOCKED and rec.reason_code == "DATA_STALE"
    assert stale.store().list_fills(stale.exp) == []
    waiting = World(tmp_path / "b", last_session=date(2026, 10, 6), now=datetime(2026, 10, 7, 20, 30, tzinfo=timezone.utc))
    rec = waiting.runtime().run_cycle()
    assert rec.status is CycleStatus.NO_NEW_BAR and rec.reason_code == "AWAITING_PROVIDER_BAR"
    assert waiting.store().list_cycles(waiting.exp) == []            # nothing persisted for a no-op


@pytest.mark.parametrize("fault,code", [(FaultKind.TIMEOUT, "DATA_UNAVAILABLE"), (FaultKind.EMPTY, "DATA_UNAVAILABLE"),
                                        (FaultKind.MALFORMED_COLUMNS, "DATA_INVALID"),
                                        (FaultKind.IMPOSSIBLE_VALUES, "DATA_INVALID")])
def test_data_failures_block_the_cycle_and_never_reach_execution(world, fault, code):
    world.script(WED, "LONG")
    world.provider.set_script([Fault(fault)])
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.BLOCKED and rec.reason_code == code
    assert world.store().list_intents(world.exp) == [] and world.store().list_fills(world.exp) == []
    assert world.portfolio().cash == 10_000.0 and not world.portfolio().positions


def test_strategy_preparation_failure_is_recorded_and_blocks_execution(world):
    ScriptedStrategy.RAISE = True
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.FAILED and rec.reason_code == "STRATEGY_ERROR"
    assert world.store().list_intents(world.exp) == []


def test_ambiguous_simultaneous_signals_are_refused(world, monkeypatch):
    def two(self, data):
        ts = pd.Timestamp(data.index.max())
        return [Signal(timestamp=ts, symbol="QQQ", direction=SignalDirection.LONG, confidence=0.5),
                Signal(timestamp=ts, symbol="QQQ", direction=SignalDirection.FLAT, confidence=0.5)]
    monkeypatch.setattr(ScriptedStrategy, "generate_signals", two)
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.BLOCKED and rec.reason_code == "AMBIGUOUS_SIGNALS"


def test_a_signal_for_another_instrument_fails_closed(world, monkeypatch):
    def wrong(self, data):
        return [Signal(timestamp=pd.Timestamp(data.index.max()), symbol="SPY", direction=SignalDirection.LONG, confidence=0.5)]
    monkeypatch.setattr(ScriptedStrategy, "generate_signals", wrong)
    assert world.runtime().run_cycle().reason_code == "SIGNAL_SYMBOL_MISMATCH"


# =====================================================================================
# L3/L8. risk, execution, portfolio, reconciliation, monitoring (end to end)
# =====================================================================================
def test_end_to_end_entry_is_risk_sized_executed_applied_reconciled_monitored_and_auditable(world):
    world.script(WED, "LONG")
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.ORDER_FILLED, rec.to_dict()
    store = world.store()
    (intent,) = store.list_intents(world.exp)
    (fill,) = store.list_fills(world.exp)
    d = rec.risk_decision
    assert d["approved"] is True and intent.quantity == d["final_approved_quantity"] <= d["final_approved_quantity"]
    assert intent.quantity > 0 and intent.state is IntentState.APPLIED and intent.client_order_id == fill.client_order_id
    assert fill.side == "BUY" and fill.quantity == intent.quantity and fill.fee == 0.0
    pf = world.portfolio()
    assert pf.positions["QQQ"].quantity == intent.quantity and pf.positions["QQQ"].entry_price == fill.fill_price
    assert pf.cash == pytest.approx(10_000.0 - intent.quantity * fill.fill_price)
    assert rec.reconciliation_status == "MATCH" and store.latest_reconciliation(world.exp).status is ReconStatus.MATCH
    assert rec.monitoring["outcome"] == "COMPLETED" and rec.monitoring["run_id"] == "mon-1"
    assert rec.monitoring["status"] == "HEALTHY" and rec.monitoring["persisted"] is True
    # provenance
    assert rec.strategy_hash == strategy_version(ScriptedStrategy) and rec.config_hash == world.config().identity_hash()
    assert rec.dataset["complete_bars_hash"] and rec.idempotency_key and rec.signal["direction"] == "LONG"
    assert rec.provenance["adapter"] == "PaperBroker" and rec.provenance["execution_mode"] == "paper"
    assert rec.provenance["execution"]["fee"] and "slippage" in rec.provenance["execution"]
    reloaded = store.get_cycle(rec.cycle_id)
    assert reloaded.to_dict() == rec.to_dict()                       # everything reloads from disk


def test_risk_rejection_prevents_any_order(tmp_path):
    w = World(tmp_path, config_over={"risk": {"allocation_per_trade_pct": 0.0001, "max_portfolio_exposure_pct": 0.5,
                                              "risk_pct_per_trade": 0.01}})
    w.script(WED, "LONG")
    rec = w.runtime().run_cycle()
    assert rec.status is CycleStatus.RISK_REJECTED and rec.risk_decision["approved"] is False
    assert w.store().list_intents(w.exp) == [] and w.store().list_fills(w.exp) == []
    assert w.portfolio().cash == 10_000.0 and not w.portfolio().positions
    assert len(w.monitor_calls) == 1                                  # a completed evaluation is monitored even with no trade
    assert rec.monitoring["outcome"] == "COMPLETED"


def test_unmodelled_short_exposure_fails_closed(world):
    world.script(WED, "SHORT")
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.RISK_REJECTED
    assert rec.reason_code in ("SHORT_NOT_MODELED",) or rec.risk_decision["approved"] is False
    assert world.store().list_intents(world.exp) == [] and not world.portfolio().positions


def test_flat_with_no_position_is_a_noop_and_a_flat_close_follows_the_close_path(world):
    world.script(WED, "FLAT")
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.NO_ACTION and rec.reason_code == "ALREADY_FLAT"
    assert rec.risk_decision["is_close"] is True
    # open, then close on the next bar
    world.script(date(2026, 10, 8), "LONG"); world.advance(date(2026, 10, 8), NEXT_DAY_AFTER_CLOSE)
    assert world.runtime().run_cycle().status is CycleStatus.ORDER_FILLED
    world.script(date(2026, 10, 9), "FLAT"); world.advance(date(2026, 10, 9), datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc))
    closing = world.runtime().run_cycle()
    assert closing.status is CycleStatus.ORDER_FILLED and closing.risk_decision["is_close"] is True
    assert closing.fill["side"] == "SELL" and not world.portfolio().positions
    assert world.store().latest_reconciliation(world.exp).status is ReconStatus.MATCH


def test_close_is_not_blocked_by_entry_exposure_limits(tmp_path):
    w = World(tmp_path)
    w.script(WED, "LONG")
    assert w.runtime().run_cycle().status is CycleStatus.ORDER_FILLED
    w.cfg_dict["risk"] = {"allocation_per_trade_pct": 0.10, "max_portfolio_exposure_pct": 0.01, "risk_pct_per_trade": 0.01}
    w.script(date(2026, 10, 8), "FLAT"); w.advance(date(2026, 10, 8), NEXT_DAY_AFTER_CLOSE)
    assert w.runtime().run_cycle().status is CycleStatus.ORDER_FILLED      # exposure already above the new limit


def test_execution_costs_are_applied_once_recorded_and_flow_into_portfolio_cash(tmp_path):
    w = World(tmp_path, config_over={"execution": {"timing": "SIGNAL_BAR_CLOSE", "slippage_bps": 10.0, "fee_bps": 20.0,
                                                   "fixed_fee": 1.0}})
    w.script(WED, "LONG")
    rec = w.runtime().run_cycle()
    f = w.store().list_fills(w.exp)[0]
    assert rec.status is CycleStatus.ORDER_FILLED
    assert f.fill_price == pytest.approx(f.reference_price * 1.001)           # BUY pays slippage
    assert f.slippage_amount == pytest.approx(f.fill_price - f.reference_price)
    notional = f.quantity * f.fill_price
    assert f.fee == pytest.approx(notional * 0.002 + 1.0)
    assert w.portfolio().cash == pytest.approx(10_000.0 - notional - f.fee)    # fee debited exactly once
    assert w.store().latest_reconciliation(w.exp).status is ReconStatus.MATCH  # broker ledger agrees
    # a round trip: total cash effect = gross pnl - both fees (net is not confused with gross)
    w.script(date(2026, 10, 8), "FLAT"); w.advance(date(2026, 10, 8), NEXT_DAY_AFTER_CLOSE)
    assert w.runtime().run_cycle().status is CycleStatus.ORDER_FILLED
    fills = w.store().list_fills(w.exp)
    gross = fills[0].quantity * (fills[1].fill_price - fills[0].fill_price)
    assert w.portfolio().cash - 10_000.0 == pytest.approx(gross - fills[0].fee - fills[1].fee)
    closed = w.portfolio().closed_positions[0]
    assert closed.realized_pnl == pytest.approx(gross)                         # position pnl stays gross of fees


def test_insufficient_cash_for_fee_is_an_order_rejection_not_a_negative_balance(tmp_path):
    w = World(tmp_path, config_over={"execution": {"timing": "SIGNAL_BAR_CLOSE", "slippage_bps": 0.0, "fee_bps": 0.0,
                                                   "fixed_fee": 50_000.0}})
    w.script(WED, "LONG")
    rec = w.runtime().run_cycle()
    assert rec.status is CycleStatus.ORDER_REJECTED and rec.reason_code == "INSUFFICIENT_CASH"
    assert w.store().list_fills(w.exp) == [] and w.portfolio().cash == 10_000.0
    (intent,) = w.store().list_intents(w.exp)
    assert intent.state is IntentState.REJECTED


def test_a_generated_signal_alone_never_changes_the_portfolio(world):
    world.script(WED, "LONG")
    rt = world.runtime()
    instance, _ = rt.resolve_strategy()
    ds = rt._fetch_dataset()
    assert instance.generate_signals(instance.prepare(ds.candles))              # a signal exists...
    assert world.portfolio().cash == 10_000.0 and not world.portfolio().positions  # ...nothing moved


def test_paper_broker_rejection_is_an_order_rejection(world, monkeypatch):
    from src.execution.engine import PaperBroker
    monkeypatch.setattr(PaperBroker, "submit_signal", lambda self, *a, **k: (_ for _ in ()).throw(ValueError("symbol busy")))
    world.script(WED, "LONG")
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.ORDER_REJECTED and rec.reason_code == "BROKER_REJECTED"
    assert world.store().list_fills(world.exp) == [] and not world.portfolio().positions


def test_risk_engine_failure_is_a_failed_cycle_with_no_order(world, monkeypatch):
    world.script(WED, "LONG")
    rt = world.runtime()
    monkeypatch.setattr(rt._risk, "decide", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("risk boom")))
    rec = rt.run_cycle()
    assert rec.status is CycleStatus.FAILED and rec.reason_code == "RISK_ENGINE_ERROR"
    assert world.store().list_intents(world.exp) == []


# =====================================================================================
# L4. idempotency and restart recovery
# =====================================================================================
def only_one_execution(w: World) -> None:
    store = w.store()
    assert len(store.list_fills(w.exp)) == 1 and len(store.list_intents(w.exp)) == 1
    fill = store.list_fills(w.exp)[0]
    assert w.portfolio().positions["QQQ"].quantity == fill.quantity               # applied exactly once
    assert w.portfolio().cash == pytest.approx(10_000.0 - fill.quantity * fill.fill_price - fill.fee)
    assert store.latest_reconciliation(w.exp).status is ReconStatus.MATCH


def test_same_cycle_twice_and_after_restart_does_not_duplicate_orders_or_fills(world):
    world.script(WED, "LONG")
    first = world.runtime().run_cycle()
    second = world.runtime().run_cycle()                         # a brand-new runtime object == restart
    assert second.replayed and second.cycle_id == first.cycle_id and second.status is CycleStatus.ORDER_FILLED
    only_one_execution(world)
    assert len(world.monitor_calls) == 1


def test_distinct_new_completed_bars_produce_distinct_cycles_and_keys(world):
    world.script(WED, "LONG")
    a = world.runtime().run_cycle()
    world.advance(date(2026, 10, 8), NEXT_DAY_AFTER_CLOSE)
    b = world.runtime().run_cycle()
    assert a.idempotency_key != b.idempotency_key and a.cycle_id != b.cycle_id
    assert b.status is CycleStatus.NO_ACTION and b.decision_bar.startswith("2026-10-08")


def test_a_revised_dataset_for_an_already_acted_bar_replays_and_cannot_create_a_second_order(world):
    world.script(WED, "LONG")
    assert world.runtime().run_cycle().status is CycleStatus.ORDER_FILLED

    class Revised(SyntheticCandleProvider):
        def fetch_candles(self, *a, **k):
            df = super().fetch_candles(*a, **k)
            df.iloc[:-1, df.columns.get_loc("close")] += 0.01           # an older bar was revised upstream
            df["high"] = df[["high", "close"]].max(axis=1)
            return df
    world.provider = FaultInjectingProvider(Revised(WED), [None])
    rec = world.runtime().run_cycle()
    assert rec.replayed is True and rec.status is CycleStatus.ORDER_FILLED       # data revision is provenance, not identity
    only_one_execution(world)


def test_a_revised_dataset_for_a_no_action_bar_replays_the_same_cycle(world):
    first = world.runtime().run_cycle()
    assert first.status is CycleStatus.NO_ACTION

    class Revised(SyntheticCandleProvider):
        def fetch_candles(self, *a, **k):
            df = super().fetch_candles(*a, **k)
            df.iloc[:-1, df.columns.get_loc("close")] += 0.01
            df["high"] = df[["high", "close"]].max(axis=1)
            return df
    world.provider = FaultInjectingProvider(Revised(WED), [None])
    again = world.runtime().run_cycle()
    assert again.replayed is True and again.cycle_id == first.cycle_id


@pytest.mark.parametrize("point", ["after_intent_persisted", "broker_before_commit", "broker_after_commit",
                                   "before_portfolio_save", "after_portfolio_save"])
def test_process_death_at_every_boundary_recovers_to_exactly_one_execution(world, point):
    world.script(WED, "LONG")
    world.faults.arm(point, SimulatedCrash(point))
    with pytest.raises(SimulatedCrash):
        world.runtime().run_cycle()                              # the process "dies" here
    rec = world.runtime().run_cycle()                            # restart: recovery from recorded state
    assert rec.status is CycleStatus.ORDER_FILLED
    only_one_execution(world)
    third = world.runtime().run_cycle()
    assert third.replayed and third.status is CycleStatus.ORDER_FILLED
    only_one_execution(world)


def test_lost_acknowledgement_is_resolved_by_ledger_lookup_not_resubmission(world):
    world.script(WED, "LONG")
    world.faults.arm("broker_after_commit", BrokerUncertain("timeout waiting for acknowledgement"))
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.AWAITING_EXECUTION and rec.reason_code == "BROKER_UNCERTAIN"
    assert len(world.store().list_fills(world.exp)) == 1         # the broker DID execute; the caller did not know
    assert not world.portfolio().positions                       # portfolio not touched on an inconclusive result
    nxt = world.runtime().run_cycle()
    assert nxt.status is CycleStatus.ORDER_FILLED
    only_one_execution(world)


def test_no_new_risk_while_an_execution_outcome_is_ambiguous(world, monkeypatch):
    world.script(WED, "LONG")
    original = ExperimentRegistry.save_portfolio

    def broken(self, *a, **k):
        raise OSError("disk unavailable")
    monkeypatch.setattr(ExperimentRegistry, "save_portfolio", broken)
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.FAILED and rec.reason_code == "INFRASTRUCTURE_ERROR"
    (intent,) = world.store().list_intents(world.exp)
    assert intent.state is IntentState.FILLED_AT_BROKER           # recorded at the broker, not yet in the portfolio
    world.advance(date(2026, 10, 8), NEXT_DAY_AFTER_CLOSE)
    world.script(date(2026, 10, 8), "LONG")
    blocked = world.runtime().run_cycle()
    assert blocked.status is CycleStatus.AWAITING_EXECUTION and blocked.reason_code == "UNRESOLVED_INTENT"
    assert len(world.store().list_fills(world.exp)) == 1 and len(world.store().list_intents(world.exp)) == 1
    monkeypatch.setattr(ExperimentRegistry, "save_portfolio", original)
    world.advance(WED, AFTER_CLOSE)
    world.runtime().run_cycle()
    only_one_execution(world)


def test_fill_and_cash_are_applied_once_even_if_recovery_runs_repeatedly(world):
    world.script(WED, "LONG")
    world.faults.arm("before_portfolio_save", SimulatedCrash("x"))
    with pytest.raises(SimulatedCrash):
        world.runtime().run_cycle()
    for _ in range(3):
        world.runtime().recover()
    only_one_execution(world)


def test_runtime_lock_prevents_two_concurrent_paper_runs(world):
    from src.monitoring.scheduler import SchedulerLock
    held = SchedulerLock(world.store_path.parent, f"paper-{world.exp}")
    held.acquire()
    try:
        with pytest.raises(PaperLockedError):
            world.runtime().run_cycle()
    finally:
        held.release()


def test_idempotency_is_enforced_by_the_database_not_just_by_code(world):
    world.script(WED, "LONG")
    rec = world.runtime().run_cycle()
    store = world.store()
    assert store.insert_cycle(rec) is False                       # UNIQUE idempotency key
    intent = store.list_intents(world.exp)[0]
    assert store.record_intent(intent, rec) is False              # UNIQUE cycle / client order id / decision bar
    fill = store.list_fills(world.exp)[0]
    again = store.record_broker_fill(intent.intent_id, world.exp, fill)
    assert again.seq == fill.seq and len(store.list_fills(world.exp)) == 1
    conn = sqlite3.connect(world.store_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO paper_broker_fills (client_order_id, experiment_id, record_json) VALUES (?,?,?)",
                     (fill.client_order_id, world.exp, "{}"))
    conn.close()


# =====================================================================================
# L5. reconciliation and discrepancies
# =====================================================================================
def test_reconciliation_mismatch_blocks_new_risk_is_persisted_and_never_overwritten(world):
    world.script(WED, "LONG")
    assert world.runtime().run_cycle().status is CycleStatus.ORDER_FILLED
    good = world.portfolio()
    tampered = Portfolio.reconstruct(cash=good.cash + 123.0, positions=list(good.positions.values()))
    world.registry.save_portfolio(world.exp, tampered)             # an unexplained external edit
    world.advance(date(2026, 10, 8), NEXT_DAY_AFTER_CLOSE)
    world.script(date(2026, 10, 8), "FLAT")
    blocked = world.runtime().run_cycle()
    assert blocked.status is CycleStatus.RECONCILIATION_REQUIRED and blocked.reason_code == "RECONCILIATION_MISMATCH"
    assert len(world.store().list_fills(world.exp)) == 1           # no corrective or new order was submitted
    assert world.portfolio().cash == pytest.approx(good.cash + 123.0)   # nothing silently "fixed"
    problems = world.store().list_reconciliations(world.exp, only_problems=True)
    assert problems and problems[0].discrepancies[0]["kind"] == "CASH"
    assert problems[0].discrepancies[0]["difference"] == pytest.approx(123.0)
    world.registry.save_portfolio(world.exp, good)                 # the operator restores the explained state
    ok = world.runtime().run_cycle()
    assert ok.status is CycleStatus.ORDER_FILLED                   # the FLAT scripted for the new bar now executes
    assert world.store().list_reconciliations(world.exp, only_problems=True)     # the mismatch history is retained


def test_unexplained_portfolio_state_at_apply_time_is_reported_not_overwritten(world):
    world.script(WED, "LONG")
    world.faults.arm("before_portfolio_save", SimulatedCrash("x"))
    with pytest.raises(SimulatedCrash):
        world.runtime().run_cycle()
    world.registry.save_portfolio(world.exp, Portfolio.reconstruct(cash=5_000.0, positions=[]))   # neither before nor after
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.RECONCILIATION_REQUIRED
    (intent,) = world.store().list_intents(world.exp)
    assert intent.state is IntentState.FILLED_AT_BROKER            # left for a human, never force-applied
    assert world.portfolio().cash == 5_000.0 and not world.portfolio().positions
    assert world.store().latest_reconciliation(world.exp).status is ReconStatus.MISMATCH


def test_reconcile_now_reports_match_pending_and_mismatch(world):
    assert world.runtime().reconcile_now().status is ReconStatus.MATCH
    world.script(WED, "LONG")
    world.faults.arm("broker_after_commit", SimulatedCrash("x"))
    with pytest.raises(SimulatedCrash):
        world.runtime().run_cycle()
    rec = world.runtime().reconcile_now()
    assert rec.status in (ReconStatus.MISMATCH, ReconStatus.PENDING) and rec.summary["intents"]["unresolved"] == 1


def test_the_broker_ledger_replays_to_the_same_state_after_a_restart(world):
    world.script(WED, "LONG")
    world.runtime().run_cycle()
    s1 = world.runtime()._gateway(world.store()).state()
    s2 = PaperTradingRuntime.__new__(PaperTradingRuntime)             # fresh objects, same files
    s2 = world.runtime()._gateway(PaperStore.open_existing(world.store_path)).state()
    assert s1 == s2 and "QQQ" in s1.positions and s1.cash == pytest.approx(world.portfolio().cash)


def test_baseline_with_an_existing_position_reconciles_and_can_be_closed(tmp_path):
    w = World(tmp_path, initialize=False)
    pos = Position(symbol="QQQ", side=PositionSide.LONG, quantity=10, entry_price=120.0,
                   entry_timestamp=pd.Timestamp("2026-09-01", tz="UTC"), entry_signal_id=uuid4())
    w.registry.save_portfolio(w.exp, Portfolio.reconstruct(cash=8_800.0, positions=[pos]))
    w.runtime().initialize_account("baseline with a position")
    assert w.runtime().reconcile_now().status is ReconStatus.MATCH
    w.script(WED, "FLAT")
    rec = w.runtime().run_cycle()
    assert rec.status is CycleStatus.ORDER_FILLED and not w.portfolio().positions
    assert w.store().latest_reconciliation(w.exp).status is ReconStatus.MATCH


# =====================================================================================
# L6. monitoring and alerts
# =====================================================================================
def test_monitoring_failure_never_changes_execution_and_never_causes_a_second_order(world):
    world.script(WED, "LONG")
    world.monitor_error = RuntimeError("monitoring database is locked")
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.ORDER_FILLED and rec.monitoring["status"] == "ERROR"
    assert "locked" in rec.monitoring["error"]
    world.monitor_error = None
    assert world.runtime().run_cycle().replayed
    only_one_execution(world)


def test_post_cycle_monitoring_uses_the_real_service_and_alert_events_follow_genuine_transitions(tmp_path):
    from src.monitoring import MonitoringService, MonitoringStore, RegistryStateSource
    from src.monitoring.alerts.models import AlertEventSettings

    w = World(tmp_path, spec_exposure=0.001, equity_points=25)      # monitoring ceiling far below the position
    mdb = tmp_path / "monitoring.db"
    settings = AlertEventSettings(enabled=True, channels=("email",))

    def factory():
        return MonitoringService(RegistryStateSource(ExperimentRegistry(w.reg_path), experiment_id=w.exp),
                                 MonitoringStore(mdb), alerts=settings)
    w.monitor = factory                                              # the real monitoring path, post-cycle
    w.script(WED, "LONG")
    rec = w.runtime().run_cycle()
    assert rec.status is CycleStatus.ORDER_FILLED and rec.monitoring["persisted"] is True
    store = MonitoringStore(mdb)
    exposure = [f for f in store.list_findings(status="OPEN") if f.rule_id == "risk.portfolio_exposure"]
    assert exposure, [f.rule_id for f in store.list_findings()]
    events = store.alerts.list_events(limit=50)
    assert [e.event_type.value for e in events].count("NEW") >= 1
    n = len(events)
    factory().run_once(); factory().run_once()                       # unchanged condition: no duplicate alerts
    assert len(store.alerts.list_events(limit=50)) == n
    assert store.alerts.counts()["DELIVERED"] == 0                   # the runtime never dispatches or sends anything


def test_blocked_and_failed_cycles_are_reported_truthfully_never_as_executed(world):
    world.script(WED, "LONG")
    world.provider.set_script([Fault(FaultKind.TIMEOUT)])
    blocked = world.runtime().run_cycle()
    assert blocked.status is CycleStatus.BLOCKED and blocked.fill is None and blocked.intent_id is None
    persisted = world.store().get_cycle(blocked.cycle_id)
    assert persisted.status is CycleStatus.BLOCKED


# =====================================================================================
# L7. architecture and safety boundaries
# =====================================================================================
PAPER_FILES = sorted((REPO / "src" / "paper").glob("*.py")) + [REPO / "src" / "cli" / "paper.py"]
FORBIDDEN_IMPORTS = ("anthropic", "src.ai", "src.broker", "src.promotion", "src.reconciliation",
                     "src.monitoring.alerts.dispatcher", "src.monitoring.alerts.transports",
                     "src.monitoring.alerts.sink", "src.strategies.promoted", "smtplib", "requests", "httpx")


def _imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_paper_modules_import_no_live_broker_llm_promotion_or_alert_delivery_code():
    assert len(PAPER_FILES) >= 8
    for f in PAPER_FILES:
        if not f.exists():
            continue
        for mod in _imports(f):
            assert not mod.startswith(FORBIDDEN_IMPORTS), (f.name, mod)


def test_paper_modules_name_no_live_adapter_and_have_no_dynamic_broker_selection():
    for f in PAPER_FILES:
        if not f.exists():
            continue
        text = f.read_text()
        for needle in ("AlpacaBroker", "IBKRBroker", "IGBroker", "TigerBroker", "BrokerConnection", "importlib",
                       "getattr(broker", "eval(", "exec("):
            assert needle not in text, (f.name, needle)


def test_the_runtime_uses_the_authoritative_risk_engine_execution_model_and_paper_broker():
    text = (REPO / "src" / "paper" / "runtime.py").read_text()
    assert "PortfolioRiskEngine" in text and "ExecutionModel" in text and "to_trade_intent" in text
    assert "decide_close" in text and "get_strategy_class" in text
    gw = (REPO / "src" / "paper" / "gateway.py").read_text()
    assert "PaperBroker.from_state" in gw or "PaperBroker" in gw
    # no second accounting / risk arithmetic in the runtime
    for needle in ("risk_pct_per_trade *", "allocation_per_trade_pct *", "max_portfolio_exposure_pct *"):
        assert needle not in text


def test_nothing_starts_on_import_and_no_unrelated_cli_starts_paper_trading(tmp_path):
    code = (
        "import socket, sys\n"
        "calls=[]\n"
        "orig=socket.socket.connect\n"
        "socket.socket.connect=lambda s,*a,**k: calls.append(a) or orig(s,*a,**k)\n"
        "import src.paper, src.paper.runtime, src.paper.store, src.paper.gateway, src.paper.reconcile\n"
        "bad=[x for x in sys.modules if x=='anthropic' or x.startswith('src.ai') or x=='smtplib'\n"
        "      or x.startswith('src.broker.alpaca') or x.startswith('src.broker.ibkr') or x.startswith('src.broker.ig')\n"
        "      or x.startswith('src.broker.tiger') or x.startswith('src.promotion')]\n"
        "assert not bad, bad\n"
        "import src.cli.__main__ as m\n"
        "assert 'paper' in m.ARGV_COMMANDS\n"
        "assert 'src.cli.paper' not in sys.modules, 'paper CLI must be lazy'\n"
        "assert not calls, calls\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
                       env={"PYTHONPATH": ":".join([str(REPO)] + [p for p in sys.path if "stubs" in p]),
                            "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert not any(tmp_path.iterdir())                                # no database or file was created


def test_paper_fixtures_embed_no_secrets_or_runtime_data():
    text = Path(__file__).read_text()
    assert ("sk-" + "ant-") not in text and ("ANTHROPIC_API_KEY" + "=") not in text
    assert not list((REPO / "src" / "paper").glob("*.db"))


# =====================================================================================
# Additional traceability / boundary evidence
# =====================================================================================
def test_every_record_traces_signal_to_risk_to_intent_to_fill_to_position(world):
    world.script(WED, "LONG")
    rec = world.runtime().run_cycle()
    store = world.store()
    (intent,) = store.list_intents(world.exp)
    (fill,) = store.list_fills(world.exp)
    position = world.portfolio().positions["QQQ"]
    assert rec.signal["id"] == intent.signal["id"] == fill.signal_id == str(position.entry_signal_id)
    assert intent.risk_decision == rec.risk_decision and intent.cycle_id == rec.cycle_id
    assert intent.client_order_id == fill.client_order_id == rec.client_order_id
    assert intent.decision_bar == rec.decision_bar and intent.execution_config == rec.provenance["execution"]


def test_the_intent_is_durable_before_anything_reaches_the_broker(world):
    world.script(WED, "LONG")
    world.faults.arm("after_intent_persisted", SimulatedCrash("x"))
    with pytest.raises(SimulatedCrash):
        world.runtime().run_cycle()
    store = world.store()
    (intent,) = store.list_intents(world.exp)
    assert intent.state is IntentState.INTENT_RECORDED and store.list_fills(world.exp) == []
    assert world.portfolio().cash == 10_000.0 and not world.portfolio().positions
    assert store.get_cycle(intent.cycle_id).status is CycleStatus.AWAITING_EXECUTION


def test_recovery_after_a_lost_acknowledgement_never_calls_gateway_submit_again(world, monkeypatch):
    from src.paper.gateway import PaperBrokerGateway
    calls = []
    real = PaperBrokerGateway.submit

    def spy(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)
    monkeypatch.setattr(PaperBrokerGateway, "submit", spy)
    world.script(WED, "LONG")
    world.faults.arm("broker_after_commit", BrokerUncertain("ack lost"))
    world.runtime().run_cycle()
    assert len(calls) == 1
    world.runtime().run_cycle()                       # recovery resolves by ledger lookup, not by submitting
    assert len(calls) == 1
    only_one_execution(world)


def test_missing_prices_are_never_valued_as_zero_after_a_fill(world):
    world.script(WED, "LONG")
    world.runtime().run_cycle()
    pf = world.portfolio()
    pos = pf.positions["QQQ"]
    assert pf.equity == pytest.approx(pf.cash + pos.quantity * pos.entry_price) and pf.equity > pf.cash


def test_no_other_package_imports_the_paper_runtime():
    offenders = []
    for pkg in ("ai", "monitoring", "dashboard", "risk", "execution", "portfolio", "strategies", "backtesting",
                "broker", "data", "calendar", "analytics", "research", "promotion"):
        for f in (REPO / "src" / pkg).rglob("*.py"):
            if any(m.startswith("src.paper") for m in _imports(f)):
                offenders.append(str(f.relative_to(REPO)))
    assert offenders == [], offenders


def test_the_monitoring_agent_gained_no_paper_trading_capability():
    for f in (REPO / "src" / "ai" / "agents" / "monitoring").glob("*.py"):
        body = f.read_text()
        assert "src.paper" not in body and "paper_run" not in body and "PaperTradingRuntime" not in body


# =====================================================================================
# Follow-up: post-cycle monitoring lifecycle (NO_ACTION cycles, persisted link, failure, retry, recovery)
# =====================================================================================
def _no_signal(world):
    ScriptedStrategy.SCRIPT.clear()


def test_a_no_action_cycle_runs_and_persists_post_cycle_monitoring(world):
    _no_signal(world)
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.NO_ACTION and rec.reason_code == "NO_SIGNAL"
    assert len(world.monitor_calls) == 1
    stored = world.store().get_cycle(rec.cycle_id)                     # the persisted record, not the in-memory one
    assert stored.monitoring["outcome"] == "COMPLETED" and stored.monitoring["run_id"] == "mon-1"
    assert stored.intent_id is None and world.store().list_fills(world.exp) == [] and world.store().list_intents(world.exp) == []


def test_a_cycle_with_a_fill_is_monitored_exactly_once(world):
    world.script(WED, "LONG")
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.ORDER_FILLED and len(world.monitor_calls) == 1
    assert world.store().get_cycle(rec.cycle_id).monitoring["run_id"] == "mon-1"


def test_replay_and_restart_never_repeat_completed_monitoring(world):
    _no_signal(world)
    first = world.runtime().run_cycle()
    again = world.runtime().run_cycle()                                # a brand-new runtime object = application restart
    assert again.replayed and again.cycle_id == first.cycle_id
    assert len(world.monitor_calls) == 1 and len(world.store().list_cycles(world.exp)) == 1
    recovered = world.runtime().monitor_cycle(first.cycle_id)           # explicit op on a completed step: a no-op
    assert recovered.monitoring["run_id"] == "mon-1" and len(world.monitor_calls) == 1


def test_a_monitoring_failure_is_recorded_distinctly_and_the_execution_outcome_is_unchanged(world):
    _no_signal(world)
    world.monitor_error = RuntimeError("monitoring database is locked")
    rec = world.runtime().run_cycle()
    stored = world.store().get_cycle(rec.cycle_id)
    assert stored.status is CycleStatus.NO_ACTION and stored.reason_code == "NO_SIGNAL"
    assert stored.monitoring["outcome"] == "FAILED" and "locked" in stored.monitoring["error"] and "run_id" not in stored.monitoring
    assert world.monitor_calls == []


def test_retrying_monitoring_after_a_failure_changes_no_order_fill_fee_or_portfolio(world):
    world.script(WED, "LONG")
    world.monitor_error = RuntimeError("boom")
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.ORDER_FILLED and rec.monitoring["outcome"] == "FAILED"
    before = (world.store().list_fills(world.exp), world.store().list_intents(world.exp), world.portfolio().cash,
              dict(world.portfolio().positions), len(world.store().list_cycles(world.exp)))
    world.monitor_error = None
    done = world.runtime().monitor_cycle(rec.cycle_id)
    assert done.monitoring["outcome"] == "COMPLETED" and done.monitoring["late"] is True and done.monitoring["attempts"] == 2
    after = (world.store().list_fills(world.exp), world.store().list_intents(world.exp), world.portfolio().cash,
             dict(world.portfolio().positions), len(world.store().list_cycles(world.exp)))
    assert before == after
    assert world.runtime().run_cycle().replayed                       # a later cycle run is still a pure replay
    only_one_execution(world)
    assert len(world.monitor_calls) == 1


def test_a_crash_between_the_cycle_and_monitoring_leaves_a_recoverable_pending_state(world):
    _no_signal(world)
    world.faults.arm("before_monitoring", SimulatedCrash("x"))
    with pytest.raises(SimulatedCrash):
        world.runtime().run_cycle()
    stored = world.store().list_cycles(world.exp)[0]
    assert stored.status is CycleStatus.NO_ACTION and stored.monitoring["outcome"] == "PENDING"
    assert world.monitor_calls == []
    done = world.runtime().monitor_cycle(stored.cycle_id)               # bounded explicit recovery: no strategy, no order
    assert done.monitoring["outcome"] == "COMPLETED" and len(world.monitor_calls) == 1
    assert len(world.store().list_cycles(world.exp)) == 1 and world.store().list_fills(world.exp) == []


def test_a_legacy_cycle_with_no_monitoring_record_is_truthful_and_can_be_monitored_only_explicitly(world):
    _no_signal(world)
    rt = world.runtime(with_monitor=False)
    rec = rt.run_cycle()
    assert rec.monitoring["outcome"] == "NOT_CONFIGURED"
    store = world.store()
    legacy = store.get_cycle(rec.cycle_id)
    legacy.monitoring = None                                           # the shape written before this lifecycle existed
    store.save_cycle(legacy)
    assert world.runtime().run_cycle().replayed and world.monitor_calls == []      # a replay never fabricates monitoring
    assert store.get_cycle(rec.cycle_id).monitoring is None
    done = world.runtime().monitor_cycle(rec.cycle_id)
    assert done.monitoring["outcome"] == "COMPLETED" and done.monitoring["late"] is True
    assert len(store.list_cycles(world.exp)) == 1 and store.list_fills(world.exp) == []


def test_monitoring_is_skipped_by_documented_policy_for_cycles_that_never_evaluated(world):
    world.script(WED, "LONG")
    world.provider.set_script([Fault(FaultKind.TIMEOUT)])
    rec = world.runtime().run_cycle()
    assert rec.status is CycleStatus.BLOCKED
    assert rec.monitoring["outcome"] == "SKIPPED" and "policy" in rec.monitoring["reason"] and world.monitor_calls == []
    with pytest.raises(PaperSetupError):
        world.runtime().monitor_cycle(rec.cycle_id)                    # not applicable to a blocked cycle


def test_monitor_cycle_rejects_unknown_cycles_and_unconfigured_runtimes(world):
    _no_signal(world)
    rec = world.runtime().run_cycle()
    with pytest.raises(PaperSetupError):
        world.runtime().monitor_cycle("does-not-exist")
    with pytest.raises(PaperSetupError):
        world.runtime(with_monitor=False).monitor_cycle(rec.cycle_id)
