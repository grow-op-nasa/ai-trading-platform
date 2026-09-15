"""The experiment specification -- src/experiments/spec.py.

Sprint 6's first step toward the platform's long-term target chain
(`ROADMAP.md`): experiment -> strategy/version -> parameters ->
dataset/version -> signals -> trades -> metrics -> attribution ->
report. `ExperimentSpec` is not that entire chain -- it's the
**reproducible starting point** of it: everything needed to say what an
experiment actually was, before any of it ran (`DECISIONS.md`,
ADR-0035).

    from src.experiments.spec import ExperimentSpec

    spec = ExperimentSpec.capture(
        strategy, candles, risk_limits,
        symbol="SPY", interval="1d", dataset_source="yfinance",
    )
    registry.save_spec(experiment_id, spec)
    ...
    stored = registry.get_spec(experiment_id)
    same_strategy = stored.reconstruct_strategy()          # rebuilt from name+params
    stored.verify_strategy_version()                        # False if the code changed since
    stored.verify_dataset(fresh_candles)                     # False if the data changed since

Three identity problems this closes, the first two described in
ADR-0035, the third added by ADR-0038 (timeframe-agnostic architecture
corrections):

1. **Strategy identity.** "EMA Cross, fast=12, slow=26" run today and
   run again after `EMACrossStrategy`'s implementation changes six
   months from now are not the same experiment, even though the name
   and parameters look identical. `strategy_version` is a hash of the
   strategy class's own source (`src/strategies/identity.py`) --
   automatic, not something an author has to remember to bump.
2. **Dataset identity.** "SPY, 2020-01-01 to 2025-01-01" run today and
   run again after the underlying vendor data is revised are not
   necessarily the same experiment either, even though the descriptor
   is identical. `dataset_fingerprint` hashes the actual candle values
   used (`src.utils.hashing.dataframe_fingerprint`), not just the
   request that produced them.
3. **Timeframe identity.** `SPY, 1d` and `SPY, 1m` are different
   experiments even when everything else about the request is
   identical -- `interval` is a required, typed `Interval` value
   (`src/data/base.py`), not a bare string an author could typo or a
   detail silently inferred from whatever `candles` happens to look
   like. This is what makes the platform's research/strategy layer
   genuinely timeframe-agnostic rather than implicitly daily-shaped
   (`DECISIONS.md`, ADR-0038).

Deliberately **not** a complete reproducibility system: no dataset
snapshotting or storage, no strategy source-code archival, no git/
package version integration, no automatic re-run scheduling. Those are
all real future work this establishes the seam for, not this round's
job (see ADR-0035's Decision section).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.data.base import Interval
from src.risk.models import RiskLimits
from src.strategies.base import Strategy
from src.strategies.identity import strategy_version
from src.strategies.registry import get_strategy_class
from src.utils.hashing import dataframe_fingerprint


@dataclass(frozen=True)
class ExperimentSpec:
    """An immutable, reproducible description of one experiment's inputs.

    Args:
        strategy_name: the registered name a strategy identifies itself
            by (`Strategy.name` / `@register_strategy`'s key). Used to
            look the class back up via `src.strategies.registry` for
            reconstruction.
        strategy_version: `src.strategies.identity.strategy_version`'s
            hash of the strategy class's source *at capture time*.
        strategy_params: the constructor keyword arguments (besides
            `symbol`) needed to reconstruct an equivalent instance --
            e.g. `{"fast": 12, "slow": 26, "confidence": 0.7}`. Sourced
            from the strategy's own `params` property when available
            (`BaseStrategy.params`, `DECISIONS.md` ADR-0035), or passed
            explicitly for strategies that don't expose one.
        symbol: the instrument this experiment ran against.
        interval: the candle timeframe this experiment ran at -- a typed
            `Interval` (`src/data/base.py`, ADR-0038), not a bare
            string. `capture()` and direct construction both accept a
            plain string (e.g. `"1d"`, `"1m"`) for convenience;
            `__post_init__` normalizes it to `Interval` immediately, so
            every consumer of a constructed `ExperimentSpec` can rely on
            `.interval` always being the enum, never a string an author
            could misspell. `SPY/1d` and `SPY/1m` are different
            experiments even when every other field matches.
        dataset_start: the timestamp of the first candle actually used
            (from the data itself, not the requested range -- these can
            differ, e.g. a range starting on a non-trading day).
        dataset_end: the timestamp of the last candle actually used.
        dataset_source: where the candles came from (e.g. `"yfinance"`).
        dataset_fingerprint: `dataframe_fingerprint()`'s hash of the
            actual candle values used -- the dataset identity seam.
        risk_config: the `RiskLimits` fields in effect (currently
            `allocation_per_trade_pct` and `max_portfolio_exposure_pct`
            -- `DECISIONS.md`, ADR-0032).
        backtest_config: reserved for `Backtester` configuration.
            Currently always `{}` -- `Backtester.run()` takes no
            configuration today (ADR-0011); this field exists so adding
            one later doesn't require widening `ExperimentSpec`'s shape.

    Raises:
        ValueError: any string field is empty, `interval` isn't a
            recognized `Interval` value, or `dataset_start` is after
            `dataset_end`.
    """

    strategy_name: str
    strategy_version: str
    strategy_params: dict[str, Any]
    symbol: str
    interval: Interval
    dataset_start: pd.Timestamp
    dataset_end: pd.Timestamp
    dataset_source: str
    dataset_fingerprint: str
    risk_config: dict[str, float]
    backtest_config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for attr in (
            "strategy_name",
            "strategy_version",
            "symbol",
            "dataset_source",
            "dataset_fingerprint",
        ):
            if not getattr(self, attr):
                raise ValueError(f"{attr} must be non-empty")
        # Normalize a plain string (e.g. "1d") to the typed Interval --
        # frozen dataclass, so __setattr__ is bypassed deliberately
        # (the standard pattern for post-init normalization on a frozen
        # dataclass). Raises ValueError via Interval's own Enum lookup
        # if the string isn't a recognized timeframe.
        object.__setattr__(self, "interval", Interval(self.interval))
        if self.dataset_start > self.dataset_end:
            raise ValueError(
                f"dataset_start ({self.dataset_start}) is after "
                f"dataset_end ({self.dataset_end})"
            )

    @classmethod
    def capture(
        cls,
        strategy: Strategy,
        candles: pd.DataFrame,
        risk_limits: RiskLimits,
        *,
        symbol: str,
        interval: Interval | str,
        dataset_source: str,
        backtest_config: dict[str, Any] | None = None,
        strategy_params: dict[str, Any] | None = None,
    ) -> "ExperimentSpec":
        """Build a spec from a strategy instance and the candles it ran
        against, immediately after (or instead of) running a backtest.

        Args:
            strategy: the strategy instance that produced (or will
                produce) this experiment's signals.
            candles: the exact OHLCV DataFrame the strategy ran against
                -- `dataset_start`/`dataset_end`/`dataset_fingerprint`
                are all derived from this, not from whatever range was
                originally requested.
            risk_limits: the `RiskLimits` in effect for this experiment.
            symbol: the instrument traded. Not read off `strategy`
                itself -- only `BaseStrategy` subclasses expose a
                `.symbol`, and this must work for any `Strategy`.
            interval: the candle timeframe `candles` was requested at
                (e.g. `Interval.MINUTE_1` or `"1m"`) -- the caller states
                this explicitly; it is never inferred from `candles`
                itself (`DECISIONS.md`, ADR-0038), since a DataFrame's
                own index spacing can be ambiguous (a thin dataset, a
                holiday gap) in a way the request that produced it isn't.
            dataset_source: where `candles` came from.
            backtest_config: reserved; defaults to `{}`.
            strategy_params: overrides `strategy.params` (from
                `BaseStrategy`) when provided -- required for a
                strategy that doesn't subclass `BaseStrategy` and so has
                no `params` property of its own.
        """
        resolved_params = (
            dict(strategy_params)
            if strategy_params is not None
            else dict(getattr(strategy, "params", {}))
        )
        return cls(
            strategy_name=strategy.name,
            strategy_version=strategy_version(type(strategy)),
            strategy_params=resolved_params,
            symbol=symbol,
            interval=interval,
            dataset_start=pd.Timestamp(candles.index.min()),
            dataset_end=pd.Timestamp(candles.index.max()),
            dataset_source=dataset_source,
            dataset_fingerprint=dataframe_fingerprint(candles),
            risk_config={
                "allocation_per_trade_pct": risk_limits.allocation_per_trade_pct,
                "max_portfolio_exposure_pct": risk_limits.max_portfolio_exposure_pct,
            },
            backtest_config=dict(backtest_config) if backtest_config else {},
        )

    def reconstruct_strategy(self) -> Strategy:
        """Rebuild a strategy instance from this spec's stored name and
        parameters alone -- the "reconstructed from its stored
        definition" requirement (`DECISIONS.md`, ADR-0035).

        Raises:
            KeyError: no strategy is registered under `strategy_name`
                (see `src.strategies.registry`) -- typically because the
                module defining it was never imported in this process.
        """
        strategy_cls = get_strategy_class(self.strategy_name)
        return strategy_cls(symbol=self.symbol, **self.strategy_params)

    def verify_strategy_version(self) -> bool:
        """`True` if the *currently registered* class for
        `strategy_name` still hashes to this spec's `strategy_version`.

        `False` means the strategy's implementation has changed since
        this experiment ran -- the exact drift ADR-0035 exists to make
        detectable instead of silently assumed away.
        """
        strategy_cls = get_strategy_class(self.strategy_name)
        return strategy_version(strategy_cls) == self.strategy_version

    def verify_dataset(self, candles: pd.DataFrame) -> bool:
        """`True` if `candles` fingerprints identically to this spec's
        recorded `dataset_fingerprint`.

        `False` means the data has changed (or this simply isn't the
        same dataset) since the experiment ran.
        """
        return dataframe_fingerprint(candles) == self.dataset_fingerprint
