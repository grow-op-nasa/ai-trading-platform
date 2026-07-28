# Project State

_Last updated: 2026-07-28 -- Sprint 2 (Indicator Engine, Market Regime
Detection, Backtesting Framework, Experiment Registry) confirmed on the
real dev machine: `pytest` -> 67 passed. Sprint 2 closed._

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

## Current Module

**Sprint 2 closed.** Indicator Engine, Market Regime Detection,
Backtesting Framework, and Experiment Registry are all code-complete,
tested, documented, and confirmed via real `pytest` on the dev machine
(67 passed). No module currently in progress -- see "Next Task."

What's left on the Market Data Service (moved to Roadmap, not
blocking Sprint 2 or 3): no data validation beyond required-column
checks (ADR-0006); caching is CSV-only and re-fetches whole ranges on
any cache-key miss (ADR-0007); no integration test suite against the
live yfinance API.

## Next Task

Sprint 3: **Strategies** -- a first real strategy (e.g. moving average
crossover) implementing the `Strategy` protocol from
`src/strategies/base.py`, exercised through the Sprint 2 Backtester and
logged via the Experiment Registry. See `ROADMAP.md`.

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
  noisier than a human-facing CLI probably wants long-term; not worth
  tuning yet since nothing consumes the console output programmatically.
- No CI (GitHub Actions or similar) running the test suite on push yet.
- Package layout (`src/` vs. `src/ai_trading_platform/`) and flat
  config constants vs. a typed `Settings` object -- both deferred to
  pre-1.0, tracked as ADR-0004 and ADR-0005.

## How to verify this file is accurate

```bash
pytest                 # should show 67 passed (7 config + 15 market data + 6 cache
                       # + 11 indicators + 10 regime + 8 backtesting + 10 experiments)
python src/main.py     # should log startup + watchlist
```
