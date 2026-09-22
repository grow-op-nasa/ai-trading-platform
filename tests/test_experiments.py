"""Tests for the Experiment Registry (src/experiments).

Uses a tmp_path-backed SQLite file per test so tests never touch the
real `data/experiments.db` and never interfere with each other.
"""

from __future__ import annotations

import pandas as pd
import pytest

from uuid import uuid4

from src.backtesting.models import Trade
from src.experiments.registry import ExperimentRegistry
from src.experiments.spec import ExperimentSpec
from src.portfolio.models import Portfolio
from src.portfolio.position import Position, PositionSide
from src.research.models import Finding, ResearchFindings, ResearchReport
from src.signals.models import Signal, SignalDirection


def make_registry(tmp_path) -> ExperimentRegistry:
    return ExperimentRegistry(db_path=tmp_path / "experiments.db")


def make_signal(**overrides) -> Signal:
    defaults = dict(
        timestamp=pd.Timestamp("2024-01-01"),
        symbol="SPY",
        direction=SignalDirection.LONG,
        confidence=0.8,
        metadata={"reason": "test"},
    )
    defaults.update(overrides)
    return Signal(**defaults)


def test_log_experiment_returns_incrementing_ids(tmp_path):
    registry = make_registry(tmp_path)

    first_id = registry.log_experiment(
        changed={"RSI.period": [14, 10]},
        metrics_before={"sharpe": 1.31},
        metrics_after={"sharpe": 1.42},
        decision="KEEP",
    )
    second_id = registry.log_experiment(
        changed={"MACD.fast": [12, 8]},
        metrics_before={"sharpe": 1.42},
        metrics_after={"sharpe": 1.38},
        decision="DISCARD",
    )

    assert second_id == first_id + 1


def test_get_experiment_round_trips_all_fields(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={"RSI.period": [14, 10]},
        metrics_before={"sharpe": 1.31, "win_rate": 0.56},
        metrics_after={"sharpe": 1.42, "win_rate": 0.59},
        decision="keep",  # lowercase on the way in
        strategy_name="sma_cross",
        notes="promising",
    )

    experiment = registry.get_experiment(experiment_id)

    assert experiment is not None
    assert experiment.id == experiment_id
    assert experiment.strategy_name == "sma_cross"
    assert experiment.changed == {"RSI.period": [14, 10]}
    assert experiment.metrics_before == {"sharpe": 1.31, "win_rate": 0.56}
    assert experiment.metrics_after == {"sharpe": 1.42, "win_rate": 0.59}
    assert experiment.decision == "KEEP"  # normalized to uppercase
    assert experiment.notes == "promising"
    assert experiment.created_at  # non-empty timestamp


def test_get_experiment_returns_none_for_missing_id(tmp_path):
    registry = make_registry(tmp_path)
    assert registry.get_experiment(999) is None


def test_invalid_decision_raises_value_error(tmp_path):
    registry = make_registry(tmp_path)

    with pytest.raises(ValueError):
        registry.log_experiment(
            changed={},
            metrics_before={},
            metrics_after={},
            decision="MAYBE",
        )


def test_list_experiments_returns_all_in_order(tmp_path):
    registry = make_registry(tmp_path)
    ids = [
        registry.log_experiment(
            changed={"x": [i, i + 1]},
            metrics_before={},
            metrics_after={},
            decision="KEEP",
        )
        for i in range(3)
    ]

    experiments = registry.list_experiments()

    assert [e.id for e in experiments] == ids


def test_list_experiments_filters_by_decision(tmp_path):
    registry = make_registry(tmp_path)
    registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="KEEP")
    registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="DISCARD")
    registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="KEEP")

    kept = registry.list_experiments(decision="KEEP")

    assert len(kept) == 2
    assert all(e.decision == "KEEP" for e in kept)


def test_list_experiments_filters_by_strategy_name(tmp_path):
    registry = make_registry(tmp_path)
    registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP",
        strategy_name="sma_cross",
    )
    registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP",
        strategy_name="rsi_reversion",
    )

    sma_only = registry.list_experiments(strategy_name="sma_cross")

    assert len(sma_only) == 1
    assert sma_only[0].strategy_name == "sma_cross"


def test_count_reflects_number_of_experiments(tmp_path):
    registry = make_registry(tmp_path)
    assert registry.count() == 0

    registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="KEEP")
    registry.log_experiment(changed={}, metrics_before={}, metrics_after={}, decision="KEEP")

    assert registry.count() == 2


def test_registry_persists_across_reconnects(tmp_path):
    db_path = tmp_path / "experiments.db"
    first_registry = ExperimentRegistry(db_path=db_path)
    experiment_id = first_registry.log_experiment(
        changed={"RSI.period": [14, 10]},
        metrics_before={"sharpe": 1.31},
        metrics_after={"sharpe": 1.42},
        decision="KEEP",
    )

    second_registry = ExperimentRegistry(db_path=db_path)
    experiment = second_registry.get_experiment(experiment_id)

    assert experiment is not None
    assert experiment.changed == {"RSI.period": [14, 10]}


def test_summary_includes_key_fields(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={"RSI.period": [14, 10]},
        metrics_before={"sharpe": 1.31, "win_rate": 0.56},
        metrics_after={"sharpe": 1.42, "win_rate": 0.59},
        decision="KEEP",
        strategy_name="sma_cross",
    )

    summary = registry.get_experiment(experiment_id).summary()

    assert f"Experiment #{experiment_id}" in summary
    assert "sma_cross" in summary
    assert "14 -> 10" in summary
    assert "KEEP" in summary


# ---------------------------------------------------------------------------
# Signal storage (DECISIONS.md, ADR-0016) -- signals are first-class rows
# here, not a separate repository.
# ---------------------------------------------------------------------------

def test_save_and_get_signals_round_trips(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    signal = make_signal(metadata={"trend": "UP", "reason": "EMA20 crossed EMA50"})

    registry.save_signals(experiment_id, [signal])
    fetched = registry.get_signals(experiment_id)

    assert len(fetched) == 1
    assert fetched[0].id == signal.id
    assert fetched[0].timestamp == signal.timestamp
    assert fetched[0].symbol == signal.symbol
    assert fetched[0].direction == signal.direction
    assert fetched[0].confidence == signal.confidence
    assert fetched[0].metadata == {"trend": "UP", "reason": "EMA20 crossed EMA50"}


def test_save_and_get_signals_round_trips_symbol_for_multiple_instruments(tmp_path):
    # Symbol lineage (DECISIONS.md, ADR-0033): persistence and
    # reconstruction must not collapse distinct symbols together.
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    spy_signal = make_signal(symbol="SPY")
    qqq_signal = make_signal(symbol="QQQ")

    registry.save_signals(experiment_id, [spy_signal, qqq_signal])
    fetched_by_id = {s.id: s for s in registry.get_signals(experiment_id)}

    assert fetched_by_id[spy_signal.id].symbol == "SPY"
    assert fetched_by_id[qqq_signal.id].symbol == "QQQ"


def test_get_signals_orders_by_timestamp(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    later = make_signal(timestamp=pd.Timestamp("2024-01-03"))
    earlier = make_signal(timestamp=pd.Timestamp("2024-01-01"))

    registry.save_signals(experiment_id, [later, earlier])
    fetched = registry.get_signals(experiment_id)

    assert [s.id for s in fetched] == [earlier.id, later.id]


def test_get_signals_only_returns_signals_for_that_experiment(tmp_path):
    registry = make_registry(tmp_path)
    experiment_a = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    experiment_b = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="DISCARD"
    )
    signal_a = make_signal()
    signal_b = make_signal()

    registry.save_signals(experiment_a, [signal_a])
    registry.save_signals(experiment_b, [signal_b])

    assert [s.id for s in registry.get_signals(experiment_a)] == [signal_a.id]
    assert [s.id for s in registry.get_signals(experiment_b)] == [signal_b.id]


def test_get_signal_fetches_by_id_directly(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    signal = make_signal()
    registry.save_signals(experiment_id, [signal])

    fetched = registry.get_signal(signal.id)

    assert fetched is not None
    assert fetched.id == signal.id


def test_get_signal_returns_none_for_missing_id(tmp_path):
    registry = make_registry(tmp_path)
    assert registry.get_signal(make_signal().id) is None


def test_signals_persist_across_reconnects(tmp_path):
    db_path = tmp_path / "experiments.db"
    first_registry = ExperimentRegistry(db_path=db_path)
    experiment_id = first_registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    signal = make_signal()
    first_registry.save_signals(experiment_id, [signal])

    second_registry = ExperimentRegistry(db_path=db_path)
    fetched = second_registry.get_signal(signal.id)

    assert fetched is not None
    assert fetched.metadata == signal.metadata
    assert fetched.symbol == signal.symbol


def make_spec(**overrides) -> ExperimentSpec:
    defaults = dict(
        strategy_name="ema_cross",
        strategy_version="deadbeef",
        strategy_params={"fast": 12, "slow": 26, "confidence": 0.7},
        symbol="SPY",
        interval="1d",
        dataset_start=pd.Timestamp("2024-01-01"),
        dataset_end=pd.Timestamp("2024-01-10"),
        dataset_source="yfinance",
        dataset_fingerprint="cafebabe",
        risk_config={"allocation_per_trade_pct": 0.1, "max_portfolio_exposure_pct": 0.5},
    )
    defaults.update(overrides)
    return ExperimentSpec(**defaults)


def test_get_spec_returns_none_when_none_saved(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    assert registry.get_spec(experiment_id) is None


def test_save_and_get_spec_round_trips(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    spec = make_spec()

    registry.save_spec(experiment_id, spec)
    fetched = registry.get_spec(experiment_id)

    assert fetched == spec


def test_save_spec_twice_replaces_rather_than_duplicates(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )

    registry.save_spec(experiment_id, make_spec(symbol="SPY"))
    registry.save_spec(experiment_id, make_spec(symbol="QQQ"))

    assert registry.get_spec(experiment_id).symbol == "QQQ"


def test_spec_persists_across_reconnects(tmp_path):
    db_path = tmp_path / "experiments.db"
    first_registry = ExperimentRegistry(db_path=db_path)
    experiment_id = first_registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )
    spec = make_spec()
    first_registry.save_spec(experiment_id, spec)

    second_registry = ExperimentRegistry(db_path=db_path)
    fetched = second_registry.get_spec(experiment_id)

    assert fetched == spec


# ---------------------------------------------------------------------------
# Sprint 9 (DECISIONS.md, ADR-0042): trades / equity curve / portfolio /
# research report persistence.
# ---------------------------------------------------------------------------


def make_trade(**overrides) -> Trade:
    defaults = dict(
        entry_time=pd.Timestamp("2024-01-01", tz="UTC"),
        exit_time=pd.Timestamp("2024-01-05", tz="UTC"),
        direction=1,
        entry_price=100.0,
        exit_price=110.0,
        entry_signal_id=uuid4(),
        exit_signal_id=uuid4(),
    )
    defaults.update(overrides)
    return Trade(**defaults)


def new_experiment(registry: ExperimentRegistry) -> int:
    return registry.log_experiment(
        changed={}, metrics_before={}, metrics_after={}, decision="KEEP"
    )


def test_get_trades_returns_empty_list_when_none_saved(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)
    assert registry.get_trades(experiment_id) == []


def test_save_and_get_trades_round_trips(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)
    trades = [
        make_trade(entry_price=100.0, exit_price=110.0),
        make_trade(
            entry_time=pd.Timestamp("2024-01-06", tz="UTC"),
            exit_time=pd.Timestamp("2024-01-08", tz="UTC"),
            direction=-1,
            entry_price=200.0,
            exit_price=190.0,
            exit_signal_id=None,
        ),
    ]

    registry.save_trades(experiment_id, trades)
    fetched = registry.get_trades(experiment_id)

    assert fetched == trades


def test_save_trades_twice_replaces_rather_than_duplicates(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)

    registry.save_trades(experiment_id, [make_trade(entry_price=100.0)])
    registry.save_trades(experiment_id, [make_trade(entry_price=200.0), make_trade(entry_price=300.0)])

    fetched = registry.get_trades(experiment_id)
    assert len(fetched) == 2


def test_trades_are_scoped_to_their_own_experiment(tmp_path):
    registry = make_registry(tmp_path)
    first_id = new_experiment(registry)
    second_id = new_experiment(registry)

    registry.save_trades(first_id, [make_trade(entry_price=100.0)])
    registry.save_trades(second_id, [make_trade(entry_price=200.0)])

    assert len(registry.get_trades(first_id)) == 1
    assert registry.get_trades(first_id)[0].entry_price == 100.0
    assert registry.get_trades(second_id)[0].entry_price == 200.0


# ---------------------------------------------------------------------------
# Sprint 11 (DECISIONS.md, ADR-0044): Trade.quantity round-trips through
# the registry -- nullable, so a LEGACY_UNIT trade (quantity=None) and a
# PORTFOLIO_RISK trade (a real quantity) are never conflated.
# ---------------------------------------------------------------------------


def test_trade_quantity_round_trips(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)
    trade = make_trade(entry_price=100.0, exit_price=110.0, quantity=20.0)

    registry.save_trades(experiment_id, [trade])
    fetched = registry.get_trades(experiment_id)

    assert fetched == [trade]
    assert fetched[0].quantity == 20.0
    assert fetched[0].gross_pnl == pytest.approx(200.0)


def test_legacy_unit_trade_quantity_persists_as_none(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)
    trade = make_trade(entry_price=100.0, exit_price=110.0)  # quantity defaults to None

    registry.save_trades(experiment_id, [trade])
    fetched = registry.get_trades(experiment_id)

    assert fetched[0].quantity is None
    assert fetched[0].gross_pnl is None


def test_mixed_quantity_and_no_quantity_trades_round_trip_independently(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)
    sized_trade = make_trade(entry_price=100.0, exit_price=110.0, quantity=50.0)
    unsized_trade = make_trade(
        entry_time=pd.Timestamp("2024-01-06", tz="UTC"),
        exit_time=pd.Timestamp("2024-01-08", tz="UTC"),
        entry_price=200.0,
        exit_price=190.0,
    )

    registry.save_trades(experiment_id, [sized_trade, unsized_trade])
    fetched = {t.entry_price: t for t in registry.get_trades(experiment_id)}

    assert fetched[100.0].quantity == 50.0
    assert fetched[200.0].quantity is None


def test_get_equity_curve_returns_empty_series_when_none_saved(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)

    curve = registry.get_equity_curve(experiment_id)

    assert isinstance(curve, pd.Series)
    assert curve.empty


def test_save_and_get_equity_curve_round_trips(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)
    index = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC", name="timestamp")
    curve = pd.Series([100_000.0, 101_000.0, 99_500.0], index=index, name="equity")

    registry.save_equity_curve(experiment_id, curve)
    fetched = registry.get_equity_curve(experiment_id)

    assert list(fetched.values) == list(curve.values)
    assert list(fetched.index) == list(curve.index)
    assert str(fetched.index.tz) == "UTC"


def test_save_equity_curve_twice_replaces_rather_than_duplicates(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)
    index = pd.date_range("2024-01-01", periods=2, freq="D", tz="UTC", name="timestamp")

    registry.save_equity_curve(experiment_id, pd.Series([1.0, 2.0], index=index))
    registry.save_equity_curve(experiment_id, pd.Series([3.0, 4.0, 5.0], index=index.append(index[-1:])))

    assert len(registry.get_equity_curve(experiment_id)) == 3


def test_get_portfolio_returns_none_when_none_saved(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)
    assert registry.get_portfolio(experiment_id) is None


def test_save_and_get_portfolio_round_trips_open_and_closed_positions(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)

    portfolio = Portfolio(cash=50_000.0)
    portfolio.open_position(
        symbol="SPY",
        side=PositionSide.LONG,
        quantity=10,
        entry_price=450.0,
        entry_timestamp=pd.Timestamp("2024-01-01", tz="UTC"),
        entry_signal_id=uuid4(),
    )
    portfolio.open_position(
        symbol="QQQ",
        side=PositionSide.SHORT,
        quantity=-5,
        entry_price=380.0,
        entry_timestamp=pd.Timestamp("2024-01-02", tz="UTC"),
        entry_signal_id=uuid4(),
    )
    portfolio.close_position("QQQ", exit_price=370.0)

    registry.save_portfolio(experiment_id, portfolio)
    fetched = registry.get_portfolio(experiment_id)

    assert fetched.cash == portfolio.cash
    assert set(fetched.positions) == {"SPY"}
    assert fetched.positions["SPY"].quantity == 10
    assert fetched.positions["SPY"].entry_price == 450.0
    assert len(fetched.closed_positions) == 1
    assert fetched.closed_positions[0].symbol == "QQQ"
    assert fetched.closed_positions[0].realized_pnl == pytest.approx(-5 * (370.0 - 380.0))


def test_save_portfolio_twice_replaces_rather_than_duplicates(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)

    registry.save_portfolio(experiment_id, Portfolio(cash=1_000.0))
    registry.save_portfolio(experiment_id, Portfolio(cash=2_000.0))

    assert registry.get_portfolio(experiment_id).cash == 2_000.0


def test_get_research_report_returns_none_when_none_saved(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)
    assert registry.get_research_report(experiment_id) is None


def test_save_and_get_research_report_round_trips(tmp_path):
    registry = make_registry(tmp_path)
    experiment_id = new_experiment(registry)
    report = ResearchReport(
        findings=ResearchFindings(
            strategy_name="ema_cross",
            findings=[Finding("Trades", "10"), Finding("Win rate", "60.0%")],
            recommendation="Promising, worth further testing.",
        ),
        narrative="EMA Cross produced 10 trades with a 60% win rate.",
        rendered_by="fallback",
        renderer_error=None,
    )

    registry.save_research_report(experiment_id, report)
    fetched = registry.get_research_report(experiment_id)

    assert fetched == report
