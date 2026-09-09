# Project State

_Last updated: 2026-09-10 -- Sprint 4 (in progress): `PaperBroker`
(`src/execution/`) simulates fills and tracks a portfolio, closing the
loop `PositionSizer` (`src/risk/`) left open -- `account_state` hands
back a real `AccountState` for the next sizing decision. Long-only
`EMACrossStrategy` (`src/strategies/ema_cross.py`), the platform's
first permanent strategy, landed just before both. Sprint 3 (Signal
Framework, Strategy SDK, Performance Attribution, AI Research Reporter)
is complete. Confirmed via real `pytest` on the dev machine: 192/192
tests pass._

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
  Version, Configuration, Market Data, Cache, Experiments DB, and API
  Keys are real, live checks; Broker Connection reports
  `NOT_IMPLEMENTED` honestly rather than a faked pass. Run via `python
  -m src.cli doctor`. See `DECISIONS.md`, ADR-0013 (original design)
  and ADR-0020 (API Keys upgraded from `NOT_IMPLEMENTED` to a real,
  always-`OK` check once the AI Research Reporter gave it something to
  report on).
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
- ✅ `DECISIONS.md`, ADR-0017 -- second standing engineering principle:
  every component produces knowledge for the next component, not a
  finished decision on its behalf.
- ✅ Strategy SDK (`src/strategies/sdk.py`) -- Sprint 3 Module 2.
  `BaseStrategy`: `self.indicator(...)`, `self.log`,
  `self.require_columns(...)`, `self.emit_signal(...)`. Deliberately
  does not own the signal-emission loop or pick a direction/confidence
  -- the strategy author still writes `prepare()`/`generate_signals()`
  in full. `Strategy` (the Protocol) is unchanged. See `DECISIONS.md`,
  ADR-0018.
- ✅ Performance Attribution (`src/attribution/`) -- Sprint 3 Module 3.
  `PerformanceAttributor.run(result, candles)` independently recomputes
  regime via `MarketRegimeEngine` (never trusts `Signal.metadata`),
  buckets each trade by trend+volatility regime **at entry**, excludes
  warmup-period trades from "Best"/"Worst Regime" rather than
  mislabeling them. Touches zero existing files (Extension Cost: 0).
  Session-of-day breakdown deliberately deferred pending ADR-0006
  timezone consistency. See `DECISIONS.md`, ADR-0019.
- ✅ AI Research Reporter (`src/research/`) -- Sprint 3 Module 4.
  `compile_findings(result, attribution)` deterministically extracts
  evidence (trade count, win rate, Sharpe, drawdown, average hold,
  best/worst regime) into a `ResearchFindings`, with a `recommendation`
  only when the worst regime bucket is an actual loser -- never a
  fabricated session-of-day claim. `ResearchReporter` renders it via a
  `FallbackNarrativeRenderer` (always available) or an optional
  `ClaudeNarrativeRenderer` (rephrases only, forbidden from inventing
  facts), auto-selected by whether `ANTHROPIC_API_KEY` is set. See
  `DECISIONS.md`, ADR-0020. Sprint 3 is complete.
- ✅ `EMACrossStrategy` (`src/strategies/ema_cross.py`) -- the platform's
  first permanent strategy, carried forward from Sprint 3's roadmap
  note into Sprint 4. Long-only: long while EMA(fast) > EMA(slow), flat
  otherwise, defaults `fast=12, slow=26`. Deliberately simple, not
  tuned for profitability -- a vehicle for exercising the Strategy SDK,
  Backtester, Performance Attribution, and AI Research Reporter
  end to end on a real (if minimal) trading idea. Replaces the
  demonstration-only `EMACrossStrategy` that previously lived solely in
  `tests/test_strategy_sdk.py`.
- ✅ Position Sizing (`src/risk/`) -- Sprint 4.
  `PositionSizer.size(signal, account, price)` sizes a `LONG`/`SHORT`
  signal at a fixed fraction of account equity
  (`RiskLimits.risk_per_trade_pct`, default 10%), the same for every
  signal regardless of `Signal.confidence`. Sizes down to remaining
  portfolio exposure headroom (`RiskLimits.max_portfolio_exposure_pct`,
  default 50% of equity) rather than rejecting outright when the full
  allocation doesn't fit; only rejects when there's no headroom left.
  Deliberately standalone from `Backtester` this round -- ADR-0011's
  single-unit execution model is untouched. Touches zero existing
  files (Extension Cost: 0). See `DECISIONS.md`, ADR-0021.
- ✅ Paper Execution (`src/execution/`) -- Sprint 4.
  `PaperBroker.submit_signal(signal, symbol, fill_price,
  sizing_decision=None)` translates a signal into an `Order`, fills it
  instantly and completely at the given price (no slippage/commission),
  and tracks cash + one open position per symbol. `account_state`
  returns a real `src.risk.AccountState` -- cash plus each position's
  signed value at entry price for `equity`, unsigned cost basis for
  `open_exposure` -- closing the loop `PositionSizer` left open. Not
  marked to market: equity reflects entry-price valuation until a
  position closes and P&L realizes into cash. Touches zero existing
  files (Extension Cost: 0). See `DECISIONS.md`, ADR-0022.

## Current Module

**Sprint 3 is complete and confirmed. Sprint 4 is in progress**:
`EMACrossStrategy`, `PositionSizer`, and `PaperBroker` are all complete
and confirmed -- 192/192 tests pass via real `pytest` on the dev
machine (Python 3.14.6).

What's left on the Market Data Service (moved to Roadmap, not
blocking Sprint 2, 3, or 4): no data validation beyond required-column
checks (ADR-0006); caching is CSV-only and re-fetches whole ranges on
any cache-key miss (ADR-0007); no integration test suite against the
live yfinance API.

## Next Task

Commit and push `PaperBroker`. After that, Sprint 4's remaining natural
step is proving the full loop end-to-end (a real strategy's signals ->
`PositionSizer` -> `PaperBroker`, driven by real candles) before moving
to Sprint 5 (Broker Connectivity). See `ROADMAP.md`.

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
- Performance Attribution has no session-of-day breakdown (morning/
  lunch/power hour) -- deliberately deferred since it would depend on
  candle timestamps reliably being in market-local time, which isn't
  guaranteed until ADR-0006 (timezone consistency) lands. Add once that
  gap closes. The AI Research Reporter inherits this gap too -- it
  cannot yet suggest anything session-related, only regime-related.
- No `ResearchReport` persistence -- reports are produced on demand from
  a `BacktestResult` + `AttributionReport` pair, not saved back into
  `ExperimentRegistry`. Natural future step, not built this round.
- `PositionSizer` and `PaperBroker` are verified against each other in
  tests, but neither is wired into `Backtester` or a real strategy loop
  yet -- `Backtester` still assumes single-unit sizing (ADR-0011). A
  real end-to-end proof (strategy -> signals -> sizer -> broker, driven
  by real candles) is a natural next step, not built here.
  Confidence-scaled sizing and a position-count-based portfolio limit
  are deferred, not rejected -- see `DECISIONS.md`, ADR-0021.
- `PaperBroker` doesn't mark positions to market -- `equity` between
  fills can understate or overstate the account's true value whenever
  an open position has moved in price. No live price feed exists for
  it to mark against yet; real broker connectivity (Sprint 5) is the
  natural point to revisit this. See `DECISIONS.md`, ADR-0022.
- `PaperBroker` supports only one open position per symbol at a time --
  opening a second raises rather than averaging/scaling into it. Also
  deferred: limit orders, partial fills, slippage, commission.
- Package layout (`src/` vs. `src/ai_trading_platform/`) and flat
  config constants vs. a typed `Settings` object -- both deferred to
  pre-1.0, tracked as ADR-0004 and ADR-0005.

## How to verify this file is accurate

```bash
pytest                    # should show 192 passed (7 config + 15 market data + 6 cache
                          # + 11 indicators + 10 regime + 11 backtesting + 16 experiments
                          # + 27 cli/doctor + 10 signals + 11 strategy_sdk + 8 attribution
                          # + 15 research + 11 ema_cross_strategy + 16 risk + 18 execution)
python src/main.py        # should log startup + watchlist
python -m src.cli doctor  # should print one line per check and end with "Everything Healthy"
```
