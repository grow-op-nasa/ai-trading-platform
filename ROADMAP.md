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

## Sprint 3 -- The Research Layer ✅ Complete

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
- ✅ **Module 2 -- Strategy SDK** (`src/strategies/sdk.py`):
  `BaseStrategy` handles setup boilerplate -- indicator access
  (`self.indicator`), logging (`self.log`), column validation
  (`self.require_columns`), signal construction (`self.emit_signal`) --
  but deliberately never the trading logic itself: the author still
  writes `prepare()`/`generate_signals()` in full, including when and
  how often to emit. An opinionated "single vectorized method" design
  was considered and explicitly rejected. See `DECISIONS.md`, ADR-0018.
  Sprint 3's own first strategy (deliberately simple: an EMA-cross or
  opening-range-breakout, chosen for how easy it is to reason about,
  not for profitability -- complexity comes after confidence in the
  platform, not before it) is still to be built as a permanent
  `src/strategies/` file -- `EMACrossStrategy` so far only exists as a
  demonstration in `tests/test_strategy_sdk.py`.
- ✅ **Module 3 -- Performance Attribution** (`src/attribution/`):
  `PerformanceAttributor.run(result, candles)` explains results, not
  just reports them -- trade count, winning trades, win rate, average
  hold time, performance broken down by regime (trend + volatility
  joined, e.g. "Trending + Low Volatility"), with best/worst regime
  called out. Regime is recomputed independently via
  `MarketRegimeEngine`, not read from `Signal.metadata`, so it works
  for every strategy. Touches zero existing files (Extension Cost: 0).
  See `DECISIONS.md`, ADR-0019.
  - ⬜ **Session breakdown** (morning / lunch / power hour) --
    deliberately deferred: only meaningful if candle timestamps are
    reliably in market-local time, which isn't guaranteed until
    ADR-0006 (timezone consistency) lands. Add once that gap closes.
- ✅ **Module 4 -- AI Research Reporter** (`src/research/`): given a
  completed backtest + attribution report, `ResearchReporter` compiles
  deterministic, evidence-grounded findings (trade count, win rate,
  Sharpe, drawdown, average hold, best/worst regime) and renders them as
  a narrative -- a template-based fallback always available, an
  optional Claude-rendered pass that may only rephrase the same
  findings, never invent new ones. A `recommendation` (e.g. "investigate
  excluding trades entered during Ranging + Volatile") is only produced
  when the evidence actually supports one -- no session-of-day claim
  is possible yet, since that axis is still deferred pending ADR-0006.
  This is a research recommendation for a person to weigh, not a
  trading decision (ADR-0017). See `DECISIONS.md`, ADR-0020.

**Success criteria:** every strategy returns a standardized `Signal`
(✅); a new strategy can be added without modifying the Backtester
(✅ as of Module 1 -- `Backtester` depends on the `Strategy`/`Signal`
contracts, not on any concrete strategy); backtests produce attribution
metrics, not just P&L (✅ as of Module 3 -- regime breakdown; session
breakdown still pending ADR-0006); every completed experiment is stored
in SQLite with reproducible metadata (✅ since Sprint 2, extended by
Module 1's signal storage); an AI-generated research report can be
produced from an experiment's results (✅ as of Module 4); the full
test suite continues to pass (✅, 147 tests as of Module 4, pending
real-machine confirmation).

Sprint 3's own first strategy was carried forward into Sprint 4 rather
than blocking Sprint 3's close, since every Sprint 3 success criterion
above is about the platform's *capability* to run and explain a
strategy, not about having shipped a specific one -- see Sprint 4,
below, where it's now built.

## Sprint 4 -- Risk & Execution ✅ Complete

- ✅ First permanent strategy: `EMACrossStrategy`
  (`src/strategies/ema_cross.py`) -- long while EMA(fast) > EMA(slow),
  flat otherwise, long-only, defaults `fast=12, slow=26`. Deliberately
  simple, not tuned for profitability -- built via the Strategy SDK
  (`BaseStrategy`) and validated through `Backtester` +
  `PerformanceAttributor` + `ResearchReporter` before any execution
  logic gets added. Replaces the demonstration-only class that
  previously lived in `tests/test_strategy_sdk.py`.
- ✅ `src/risk/`: `PositionSizer` -- position sizing at a fixed fraction
  of account equity (`RiskLimits.risk_per_trade_pct`, default 10%),
  identical regardless of `Signal.confidence`; a portfolio-level
  exposure cap (`RiskLimits.max_portfolio_exposure_pct`, default 50%)
  that sizes down before it ever rejects outright. Deliberately
  standalone from `Backtester` this round -- see `DECISIONS.md`,
  ADR-0021. Confidence-scaled sizing and a position-count-based
  portfolio limit are deferred, not rejected.
- ✅ `src/execution/`: `PaperBroker` -- translates a `Signal` +
  `SizingDecision` into an `Order`, fills it instantly and completely
  at the caller-supplied price (no slippage/commission), tracks cash
  and one open position per symbol, and exposes `account_state` as a
  real `src.risk.AccountState` -- closing the loop `PositionSizer` left
  open. Not marked to market. See `DECISIONS.md`, ADR-0022. Real broker
  connectivity, limit orders, partial fills, and multi-position
  averaging are deferred, not rejected.
- ✅ Full loop proven end-to-end
  (`tests/test_integration_paper_trading.py`): `EMACrossStrategy`'s
  real signals over real candles, fed through `PositionSizer` then
  `PaperBroker` exactly as a future live/paper trading loop would. No
  new production code -- `src/risk` and `src/execution` stay standalone
  modules; this is proof they compose correctly, not a new
  orchestration layer. Confirms position sizing reflects equity *at the
  time of each signal* (not a stale starting value) by asserting a
  signal sized after a completed round trip uses the account's
  post-trade equity.

Sprint 4 is now feature-complete: a real strategy, sized positions, and
simulated execution all compose end to end. What's left before Sprint 5
is a judgment call, not a fixed scope -- e.g. a reusable orchestration
layer (a `PaperTradingLoop` or similar) only becomes worth building
once there's a second real caller that needs one.

## Sprint 5 -- Broker Connectivity (in progress)

- ✅ `src/broker/`: `BrokerConnection` (analogous to `DataProvider`) +
  `AlpacaBroker` -- `get_account() -> src.risk.AccountState`, reusing
  the platform's existing account-state currency. Defaults to Alpaca's
  **paper** endpoint; the live endpoint needs an explicit override.
  Built and tested against a fake HTTP session -- no real Alpaca
  account yet; real credentials plug in later via
  `ALPACA_API_KEY`/`ALPACA_API_SECRET` with zero code changes. `atp
  doctor`'s Broker Connection check is now real, gated on those being
  configured. See `DECISIONS.md`, ADR-0023.
- ✅ Order submission against a real broker -- `OrderRequest`/
  `BrokerOrder`/`OrderStatus` (`src/broker/models.py`), kept
  deliberately independent of `src/execution`'s `Order`/`Fill` model
  rather than importing it (would run the intended dependency direction
  backwards -- see `DECISIONS.md`, ADR-0024). `submit_order()`/
  `get_order()` round out `BrokerConnection`; `AlpacaBroker` implements
  both. Market orders only, submit + status check only -- order
  cancellation is not yet built.
- ✅ Order cancellation -- `BrokerConnection.cancel_order(id) -> None`,
  rounding out order management to submit + status + cancel.
  `AlpacaBroker.cancel_order()` calls Alpaca's `DELETE /v2/orders/{id}`
  and returns `None` on success -- confirms only that the broker
  accepted the cancellation, not that the order actually ended up
  canceled, since real cancellation is asynchronous. Callers wanting
  the actual outcome call `get_order()` afterward. See `DECISIONS.md`,
  ADR-0025.
- ✅ A second broker (Interactive Brokers) -- `IBKRBroker` proves
  `BrokerConnection` is actually swappable: a real second
  implementation, different transport (Client Portal Web API vs.
  Alpaca's plain REST), different credential model (browser-session
  gateway auth vs. API key/secret), no shared code with `AlpacaBroker`
  beyond the interface and the common `BrokerError` hierarchy.
  `get_account()` only this round -- `submit_order`/`get_order`/
  `cancel_order` raise `NotImplementedError` until IB's order-placement
  flow (contract id lookup, reply/confirmation handling) gets its own
  design pass. See `DECISIONS.md`, ADR-0026.
- ⬜ Interactive Brokers order submission/status/cancel -- the piece
  ADR-0026 deliberately deferred, given IB's added complexity (conid
  lookup, reply/confirmation). **On hold:** IB geo-restricts account
  access for this deployment (OFAC/Section 311 special measures) --
  confirmed inaccessible for real use, so further investment here isn't
  currently worthwhile.
- ✅ Reconciling `PaperBroker`'s simulated fills against a real
  broker's actual fills -- `src/reconciliation/`'s `reconcile_fill()`
  compares a simulated `Fill` against a real `BrokerOrder`: side-
  normalized price slippage, quantity shortfall for partial fills,
  dollar cost impact. Single-order primitive; aggregating across many
  trades into a summary report remains a natural future step. See
  `DECISIONS.md`, ADR-0027.
- ✅ A third broker (IG) -- and the first second-broker candidate the
  platform's user can actually use with a real account, since IG
  doesn't geo-restrict access the way IB does. `IGBroker`: plain REST
  (no local gateway), a third distinct credential/auth model (API key +
  username + password, exchanged once for cached session tokens).
  `get_account()` only this round -- IG's deal-reference/confirm order
  model doesn't fit `BrokerOrder`/`OrderStatus` without its own design
  pass. See `DECISIONS.md`, ADR-0028.
- ✅ IG order submission -- `IGBroker.submit_order()` places a market
  order and resolves it **synchronously** (`POST /positions/otc` then
  `GET /confirms/{dealReference}`), returning a final `FILLED`/
  `REJECTED` `BrokerOrder` directly. `get_order`/`cancel_order` stay
  `NotImplementedError` -- there's no live status to poll for and
  nothing left to cancel once a market order has resolved. Order
  currency is read from the account itself, never guessed. See
  `DECISIONS.md`, ADR-0029.
- ⬜ Tiger Brokers/Tiger Trade as a fourth broker -- another real,
  usable account (real equities, RSA-signed request auth) the user
  could exercise; not started.

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
