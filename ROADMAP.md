# Roadmap

Forward-looking and expected to shift. When a sprint's scope changes,
update this file and note the reasoning in `DECISIONS.md` if it's a
real architectural pivot rather than routine re-scoping. For what's
actually done, see `PROJECT_STATE.md` and `CHANGELOG.md`.

## Sprint 0 -- Foundation ✅ Complete

Environment, project scaffold, capability-based `src/` layout,
Market Data Service v1.

## Sprint 1 -- Core Configuration & Market Data ~90% complete

- ✅ Config layer (`src/config`)
- ✅ Application entrypoint (`src/main.py`)
- ✅ Period-based fetching (`MarketDataService.get_history`)
- ⬜ Incremental/append-based caching (currently whole-range CSV cache)
- ⬜ Integration test suite against the live yfinance API (separate
  from the network-free unit suite)

## Sprint 2 -- Indicators (planned)

- `src/indicators/`: moving averages (SMA/EMA), RSI, MACD, Bollinger
  Bands -- each a pure function of a candles DataFrame in, a Series/
  DataFrame of indicator values out. No side effects, no I/O.
- Indicators should compose: an indicator built from indicators (e.g.
  MACD from two EMAs) should be able to call the other indicator
  functions directly.

## Sprint 3 -- Strategies (planned)

- `src/strategies/`: a strategy interface that takes candles +
  indicator values and produces a signal (buy/sell/hold, or a target
  position). First strategy is intentionally simple (e.g. moving
  average crossover) to validate the interface before anything
  sophisticated is built on top of it.
- Backtesting harness (likely via `vectorbt`, already installed)
  wired to `MarketDataService` and `src/strategies`.

## Sprint 4 -- Risk & Execution (planned)

- `src/risk/`: position sizing, per-trade and portfolio-level exposure
  limits, given a signal and account state.
- `src/execution/`: translates a sized signal into orders. Paper
  execution first; real broker connectivity comes after.

## Sprint 5 -- Broker Connectivity (planned)

- `src/broker/`: first real broker/exchange integration (candidates:
  Alpaca for equities, Interactive Brokers for broader access). Behind
  an interface analogous to `DataProvider`, so the specific broker is
  swappable the same way the data vendor is.

## Sprint 6 -- Analytics & Dashboard (planned)

- `src/analytics/`: backtest performance metrics (Sharpe, drawdown,
  win rate) and live P&L tracking.
- `src/dashboard/`: Streamlit UI over `data`, `strategies`, and
  `analytics` -- a way to see the system running, not a place where
  new logic lives.

## Sprint 7+ -- AI (planned)

- `src/ai/`: ML/LLM-based signal generation, consumed by
  `src/strategies` as one signal source among others (see
  `DECISIONS.md`, ADR-0001) -- not a rewrite of the strategy layer.
  Specific approach (classic ML on engineered features vs. LLM-based
  reasoning over market context) to be decided closer to the sprint,
  once indicators and strategies exist to feed it.

## Ongoing, not sprint-scoped

- CI running `pytest` on every push (not set up yet).
- Documentation set (`PROJECT_STATE.md`, `ARCHITECTURE.md`,
  `CHANGELOG.md`, `DECISIONS.md`, `ROADMAP.md`) updated every sprint.
