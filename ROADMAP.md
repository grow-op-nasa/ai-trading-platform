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
- ⬜ Data validation layer -- timezone consistency, duplicate/missing
  timestamps, negative prices, zero volume, sorted index (`DECISIONS.md`,
  ADR-0006)
- ⬜ Incremental cache fetch -- fetch only missing candles, merge,
  validate, save, instead of whole-range refetch on any cache-key miss
  (`DECISIONS.md`, ADR-0007; supersedes the caching mechanics in ADR-0002)
- ⬜ Integration test suite against the live yfinance API (separate
  from the network-free unit suite)

## Before v1.0 -- Packaging & Configuration hardening (planned)

Two structural improvements, deliberately deferred rather than done
mid-sprint, since both touch nearly every import statement in the
codebase and are cheaper to do as one dedicated pass than interleaved
with feature work. Full rationale in `DECISIONS.md`.

- **Standard package layout** (ADR-0004): rename `src/` to
  `src/ai_trading_platform/`, add real packaging metadata to
  `pyproject.toml`, install editable (`pip install -e .`). Resolves the
  existing dual import-convention wart (`config.x` in `main.py` vs.
  `src.config.x` everywhere else).
- **Typed `Settings` object** (ADR-0005): replace the flat constants in
  `src/config/settings.py` with a validated settings object
  (`settings.watchlist` instead of importing `WATCHLIST` by name),
  enabling environment-specific config (development / paper trading /
  live trading) and fail-fast validation. Could optionally grow a
  provider-selection factory (read a vendor name from settings/env,
  construct the matching `DataProvider`) as a nice-to-have -- ADR-0014
  (Extension Cost) noted that adding a second data vendor today means
  editing whatever call site constructs `MarketDataService(provider=...)`,
  a small and reasonable cost, not a problem that needs solving before
  this is worth doing.

Best sequenced together, since both land in the same files.

## Sprint 2 -- Research Engine ✅ Complete

Pivoted from a narrow "build indicators" sprint into a broader
quantitative research framework, on the reasoning that strategies are
cheap to write once there's infrastructure to compute indicators
uniformly, classify the market regime, backtest objectively, and record
what was learned -- so that infrastructure comes first. See
`DECISIONS.md`, ADR-0009.

- ✅ **Module 1 -- Indicator Engine** (`src/indicators/`): the only place
  indicators are calculated. `IndicatorEngine(candles).calculate("RSI",
  period=14)`. New indicators register themselves; no strategy computes
  an indicator itself. SMA, EMA, RSI, ATR, MACD, VWAP registered.
- ✅ **Module 2 -- Market Regime Detection** (`src/regime/`): continuous
  `[0, 1]` scoring across all six regimes (Trending / Ranging /
  Volatile / Low Volatility / Risk-On / Risk-Off), built on top of the
  Indicator Engine -- not AI. Trend and volatility scoring implemented;
  risk_on/risk_off return `NaN` until a VIX/macro data source exists
  (`DECISIONS.md`, ADR-0010).
- ✅ **Module 3 -- Backtesting Framework** (`src/backtesting/`,
  `src/strategies/`): `Backtester.run()` against any strategy
  conforming to the `Strategy` protocol (`src/strategies/base.py`) --
  collects trades, computes an equity curve, calculates metrics
  (Sharpe, max drawdown, win rate), generates a report. Deliberately
  simplified execution model; realistic execution deferred to Sprint 4
  (`DECISIONS.md`, ADR-0011).
- ✅ **Module 4 -- Experiment Registry** (`src/experiments/`): every
  backtest run (what changed, what result, what decision) becomes a
  permanent, queryable record -- e.g. "changed RSI period 14 -> 10,
  Sharpe 1.31 -> 1.42, win rate 56% -> 59%, decision: KEEP." SQLite-
  backed (`DECISIONS.md`, ADR-0012).

All four modules confirmed via real `pytest` on the dev machine (67
passed). Sprint 2 is closed.

## Pre-Sprint 3 -- System health check ✅ Complete

`atp doctor` (`src/cli/`, `DECISIONS.md` ADR-0013): Python Version,
Configuration, Market Data, Cache, Experiments DB are real checks;
Broker Connection and API Keys report `NOT_IMPLEMENTED` until Sprint 5
and a keyed provider exist, respectively. Run today as `python -m
src.cli doctor`; a bare `atp` command depends on the packaging work in
ADR-0004.

## Sprint 3 -- The Research Layer (in progress)

Where Sprint 1 proved the codebase could be maintainable and Sprint 2
proved it could produce reusable research infrastructure, Sprint 3
proves the platform can systematically discover trading opportunities
-- still with no broker involved. By the end of this sprint the
platform should be able to answer: given historical data, what
opportunity exists, how confident are we, how did it perform
historically, and what evidence supports it. Guided by both standing
principles in `DECISIONS.md` (ADR-0000: extensibility; ADR-0017: every
component produces knowledge for the next).

- ✅ **Module 1 -- Signal Framework** (`src/signals/`): `Signal`
  (`timestamp`, `direction`, `confidence`, `metadata`, `id`) and
  `SignalDirection` (`LONG`/`SHORT`/`FLAT`). No price field -- a Signal
  is a decision, not a market event or an order. Strategies emit
  signals sparsely (one per decision point), and the Backtester
  (`src/strategies/base.py`, `src/backtesting/`) was updated to consume
  them, superseding Sprint 2's dense `SIGNAL_COLUMN` contract. `Trade`
  references its signals by id; the Experiment Registry stores the
  actual `Signal` objects. See `DECISIONS.md`, ADR-0015, ADR-0016.
- ⬜ **Module 2 -- Strategy SDK** (planned): a base class that handles
  everything except the trading logic -- validation, indicator access,
  logging, metadata, `Signal` construction -- so a strategy author only
  writes the edge. Sprint 3's own first strategy (deliberately simple:
  an EMA-cross or opening-range-breakout, chosen for how easy it is to
  reason about, not for profitability -- complexity comes after
  confidence in the platform, not before it) will be the first thing
  built on top of this SDK.
- ⬜ **Module 3 -- Performance Attribution** (planned): backtests
  explain results, not just report them -- trade count, win rate,
  average hold time, performance broken down by regime (e.g. best:
  trending + low volatility, worst: high volatility) and by session
  (morning / lunch / power hour). Builds on `BacktestResult.signals`
  and `Trade.entry_signal_id` from Module 1 to trace outcomes back to
  the evidence behind each trade.
- ⬜ **Module 4 -- AI Research Reporter** (planned): given a completed
  experiment, generate an evidence-based research summary (e.g. "losses
  clustered in the first 20 minutes after open; next experiment:
  exclude trades before 09:50") -- a research recommendation for a
  person to weigh, not a trading decision (ADR-0017).

**Success criteria:** every strategy returns a standardized `Signal`
(✅); a new strategy can be added without modifying the Backtester
(✅ as of Module 1 -- `Backtester` depends on the `Strategy`/`Signal`
contracts, not on any concrete strategy); backtests produce attribution
metrics, not just P&L (Module 3); every completed experiment is stored
in SQLite with reproducible metadata (✅ since Sprint 2, extended by
Module 1's signal storage); an AI-generated research report can be
produced from an experiment's results (Module 4); the full test suite
continues to pass (✅, 112 tests as of Module 1).

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

- CI running `pytest` (and `python -m src.cli doctor`, now that it
  exists and returns a real exit code) on every push -- not set up yet.
- Documentation set (`PROJECT_STATE.md`, `ARCHITECTURE.md`,
  `CHANGELOG.md`, `DECISIONS.md`, `ROADMAP.md`) updated every sprint.
