# Project State

_Last updated: 2026-07-28 -- Sprint 3 Module 1 (Signal Framework)
confirmed on the real dev machine: `pytest` -> 112 passed.
`Signal`/`SignalDirection`, `Strategy`/`Backtester` updated to the new
sparse-signal contract, `Signal` storage added to the Experiment
Registry._

This file is a snapshot, not a history. It should always describe where
the project stands right now. For how we got here, see `CHANGELOG.md`.
For why things were built the way they were, see `DECISIONS.md`.

## Completed

- ✅ Environment (Homebrew, Git, Python 3.14, VS Code)
- ✅ Project structure (`src/` organized by capability, not by strategy)
- ✅ Virtual environment + dependencies (`requirements.txt`)
- ✅ Core configuration layer (`src/config/settings.py`, `src/config/logging.py`)
- ✅ Application entrypoint (`src/main.py`)
- ✅ Market Data Service v1 -- `DataProvider` interface, Yahoo Finance
  implementation, CSV caching, date-range fetching (`get_candles`)
- ✅ Market Data Service v2 -- period-based fetching (`get_history`,
  e.g. `service.get_history("SPY", period="2y", interval="5m")`),
  wired to config defaults
- ✅ Pushed to GitHub -- `github.com/grow-op-nasa/ai-trading-platform`,
  `main` tracking `origin/main`
- ✅ `CacheManager` (`src/utils/cache.py`) extracted out of
  `MarketDataService` -- generic key -> DataFrame on-disk cache,
  reusable by future capabilities (news, options chains, VIX, macro
  data, earnings, forex). See `DECISIONS.md`, ADR-0008.
- ✅ Sprint 2 pivoted to a four-module research engine (Indicator
  Engine, Market Regime Detection, Backtesting Framework, Experiment
  Registry) before any real strategy is built. See `DECISIONS.md`,
  ADR-0009.
- ✅ Indicator Engine (`src/indicators/`) -- `IndicatorEngine(candles)
  .calculate(name, **params)`, the only place indicators are
  calculated. SMA, EMA, RSI, ATR, MACD, VWAP registered today; new
  indicators register themselves without touching the engine.
- ✅ Market Regime Detection (`src/regime/`) -- `MarketRegimeEngine`
  scores every candle against all six named regimes (trending,
  ranging, volatile, low_volatility, risk_on, risk_off) as continuous
  `[0, 1]` scores; risk axis is `NaN` until a VIX/macro source exists.
  See `DECISIONS.md`, ADR-0010.
- ✅ Backtesting Framework (`src/backtesting/`, `src/strategies/`) --
  `Strategy` as a `typing.Protocol` (`name`, `prepare()`,
  `generate_signals()`); `Backtester.run()` produces a `BacktestResult`
  (trades, equity curve, metrics). Simplified execution model (no
  costs/slippage, single unit size), no-lookahead by construction. See
  `DECISIONS.md`, ADR-0011.
- ✅ Experiment Registry (`src/experiments/`) -- `ExperimentRegistry`,
  SQLite-backed (stdlib `sqlite3`), logs every backtest run: what
  changed, metrics before/after, decision (KEEP/DISCARD/INCONCLUSIVE).
  See `DECISIONS.md`, ADR-0012.
- ✅ Sprint 2 (Research Engine) -- all four modules complete: Indicator
  Engine, Market Regime Detection, Backtesting Framework, Experiment
  Registry.
- ✅ `DECISIONS.md`, ADR-0000 -- the project's architectural north star
  made explicit: every new market, indicator, strategy, broker, or AI
  model should be an extension, not a rewrite. "Does this make future
  extensions easier or harder?" is now the standing test for any
  unclear design decision.
- ✅ `atp doctor` (`src/cli/`) -- a full system health check: Python
  Version, Configuration, Market Data, Cache, and Experiments DB are
  real, live checks; Broker Connection and API Keys report
  `NOT_IMPLEMENTED` honestly rather than a faked pass. Run via `python
  -m src.cli doctor`. See `DECISIONS.md`, ADR-0013.
- ✅ `DECISIONS.md`, ADR-0014 -- Extension Cost adopted as a standing
  awareness habit (not a pass/fail target): every addition of a new
  indicator/strategy/broker/data vendor gets an `Extension Cost: N
  file(s) changed` line in `CHANGELOG.md`. The real signal is
  disproportionate cost (e.g. touching half the codebase for one
  feature), not missing an exact number.
- ✅ Signal Framework (`src/signals/`) -- Sprint 3 Module 1. `Signal`
  (frozen: `timestamp`, `direction`, `confidence`, `metadata`, `id`)
  and `SignalDirection` (`LONG`/`SHORT`/`FLAT`). Strategies emit a
  sparse `list[Signal]` -- one per decision point -- not a dense
  DataFrame column. No `price` field: a Signal is a decision, not a
  market event or an order. Supersedes ADR-0011. See `DECISIONS.md`,
  ADR-0015.
- ✅ `Strategy`/`Backtester`/`Trade` updated for the Signal contract --
  `generate_signals() -> list[Signal]`; `Trade.entry_signal_id` /
  `exit_signal_id: UUID | None` reference signals rather than
  embedding them; `BacktestResult.signals` carries the full list.
- ✅ Experiment Registry now stores `Signal`s as first-class rows
  (`save_signals`/`get_signals`/`get_signal`), not in a separate
  repository -- `log_experiment()`'s signature is untouched. See
  `DECISIONS.md`, ADR-0016.

## Current Module

**Sprint 3, Module 1 (Signal Framework) complete and confirmed.**
`atp doctor` and all of Sprint 2 are closed. Modules 2-4 of Sprint 3
(Strategy SDK, Performance Attribution, AI Research Reporter) are not
started -- see "Next Task."

What's left on the Market Data Service (moved to Roadmap, not
blocking Sprint 2 or 3): no data validation beyond required-column
checks (ADR-0006); caching is CSV-only and re-fetches whole ranges on
any cache-key miss (ADR-0007); no integration test suite against the
live yfinance API.

## Next Task

Commit and push the Signal Framework. After that, Sprint 3 Module 2:
**Strategy SDK** -- a base class that handles validation, indicator
access, logging, and `Signal` construction, so a strategy author only
writes the trading logic. Sprint 3's own first deliverable after that
is deliberately simple: an EMA-cross or opening-range-breakout
strategy, chosen for how easy it is to reason about, not for
profitability. See `ROADMAP.md`.

## Known Issues

- None blocking. `src/data/market_data.py` was proposed by an external
  lesson plan as a second, simpler `MarketDataService`; we decided
  (see `DECISIONS.md`, ADR-0003) to extend the existing service instead,
  so that file does not exist and is not needed.

## Technical Debt

- No data validation beyond required-column checks -- timezone
  consistency, duplicate/missing timestamps, negative prices, zero
  volume, and sorted index are not verified, especially on the
  cache-read path. Tracked as ADR-0006, next up.
- Cache is keyed per exact `(symbol, interval, start, end)` range, so a
  shifted date range fully misses the cache and re-fetches everything.
  Tracked as ADR-0007, next up.
- Cache is CSV, not Parquet -- fine at current data volumes, but slower
  and larger on disk once indicators/backtests pull years of intraday
  data. Now that `src/indicators/` exists, worth measuring once Module 3
  (backtesting) is pulling years of data through it.
- `loguru`'s default console format (timestamp + level + file:line) is
  noisier than a human-facing CLI probably wants long-term. This is no
  longer purely hypothetical: `atp doctor`'s Market Data and Cache
  checks now interleave DEBUG log lines with the health report itself
  (visible in a real run). Not fixed yet -- candidates are quieting the
  console sink during `atp doctor` specifically, or raising its default
  level -- but it's now a real, user-visible rough edge, not just a
  someday concern.
- No CI (GitHub Actions or similar) running the test suite on push yet.
- Package layout (`src/` vs. `src/ai_trading_platform/`) and flat
  config constants vs. a typed `Settings` object -- both deferred to
  pre-1.0, tracked as ADR-0004 and ADR-0005.

## How to verify this file is accurate

```bash
pytest                    # should show 112 passed (7 config + 15 market data + 6 cache
                          # + 11 indicators + 10 regime + 11 backtesting + 16 experiments
                          # + 26 cli/doctor + 10 signals)
python src/main.py        # should log startup + watchlist
python -m src.cli doctor  # should print one line per check and end with "Everything Healthy"
```
