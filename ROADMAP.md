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

- **Direct-vs-transitive dependency separation**: `requirements.txt`
  today is a full environment freeze (includes Jupyter, Streamlit,
  plotting/notebook tooling, and their own transitive dependencies),
  not a curated list of this platform's direct runtime dependencies.
  Flagged in the architecture review cleanup (pre-Sprint 6) but
  deliberately not touched then -- splitting it into direct vs.
  dev/transitive requires verifying nothing genuinely needed at runtime
  gets dropped, real risk of breaking a working environment for a
  cosmetic improvement. Left as documented pre-v1.0 debt, the same
  "tracked, not yet built" posture as ADR-0004's package-layout
  migration, rather than a disruptive change made in an unrelated
  cleanup pass.

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
  (`timestamp`, `symbol`, `direction`, `confidence`, `metadata`, `id` --
  `symbol` added in the pre-Sprint 6 architecture review, `DECISIONS.md`
  ADR-0033) and `SignalDirection` (`LONG`/`SHORT`/`FLAT`). No price
  field -- a Signal
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
  trading decision (ADR-0017). A `ClaudeNarrativeRenderer` failure
  (missing package, bad key, network error, malformed response, timeout)
  still falls back to the deterministic renderer rather than losing the
  report, and the failure is now recorded, not silently hidden --
  `ResearchReport.renderer_error` (pre-Sprint 6 architecture review,
  `DECISIONS.md` ADR-0034) distinguishes "fell back because Claude
  actually failed" from "fell back because it was simply never
  configured." See `DECISIONS.md`, ADR-0020, ADR-0034.

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
  of account equity (`RiskLimits.allocation_per_trade_pct`, default 10%
  -- renamed from `risk_per_trade_pct` in the pre-Sprint 6 architecture
  review, `DECISIONS.md` ADR-0032, since it's capital allocation, not a
  maximum-loss guarantee), identical regardless of `Signal.confidence`;
  a portfolio-level exposure cap (`RiskLimits.max_portfolio_exposure_pct`,
  default 50%) that sizes down before it ever rejects outright.
  Deliberately standalone from `Backtester` this round -- see
  `DECISIONS.md`, ADR-0021. Confidence-scaled sizing and a
  position-count-based portfolio limit are deferred, not rejected. True
  risk-based (stop-distance) sizing is a distinct, unbuilt capability
  (ADR-0032).
- ✅ `src/execution/`: `PaperBroker` -- translates a `Signal` +
  `SizingDecision` into an `Order`, fills it instantly and completely
  at the caller-supplied price (no slippage/commission), tracks cash
  and one open position per symbol, and exposes `account_state` as a
  real `src.portfolio.AccountState` (moved out of `src/risk` in the
  pre-Sprint 6 architecture review, `DECISIONS.md` ADR-0031) -- closing
  the loop `PositionSizer` left open. **Not marked to market** --
  `account_state.equity` values each open position at its own frozen
  entry price, never a live/current price; this must not become the
  unexamined foundation of live multi-position risk sizing before real
  mark-to-market exists (see `DECISIONS.md`, ADR-0022, strengthened by
  ADR-0031). See `DECISIONS.md`, ADR-0022. Real broker connectivity,
  limit orders, partial fills, and multi-position averaging are
  deferred, not rejected.
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

## Sprint 5 -- Broker Connectivity ✅ Complete

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
- ✅ Tiger Brokers/Tiger Trade as a fourth broker -- another real,
  usable account (real equities). `TigerBroker` wraps the official
  `tigeropen` SDK rather than hand-rolling RSA-signed request auth --
  the first broker in this codebase built on a vendor SDK.
  `get_account()` only this round -- `submit_order`/`get_order`/
  `cancel_order` raise `NotImplementedError` until Tiger's order
  lifecycle gets its own design pass. See `DECISIONS.md`, ADR-0030.
- ⬜ Tiger Trade order submission/status/cancel -- the piece ADR-0030
  deliberately deferred, even though Tiger's API looks better suited to
  a real order lifecycle than IG's does; not started.

## Pre-Sprint 6 -- Architecture review cleanup ✅ Complete

A targeted correction pass, not a redesign -- four architectural
inconsistencies an architecture review surfaced in Sprint 4-5's work,
fixed before starting Sprint 6 rather than carried forward. Every
existing ADR's deliberate deferral (confidence-scaled sizing,
stop-based risk, real mark-to-market, Tiger/IB order lifecycles, IG's
structural `get_order`/`cancel_order` gap, etc.) was left exactly as
deferred -- none of that scope moved.

- ✅ **Dependency direction**: `AccountState` moved out of `src/risk`
  into a new, neutral `src/portfolio` package that depends on nothing
  else in this codebase -- `src/broker` no longer imports `src/risk` to
  describe an account. See `DECISIONS.md`, ADR-0031.
- ✅ **Risk terminology**: `RiskLimits.risk_per_trade_pct` renamed to
  `allocation_per_trade_pct` -- it always meant capital allocation, not
  maximum loss, and there is still no stop-loss/risk-distance model in
  this codebase. Behavior unchanged; naming corrected. See
  `DECISIONS.md`, ADR-0032.
- ✅ **Signal identity**: `Signal` gained a required, first-class
  `symbol` field, threaded through `BaseStrategy`/`EMACrossStrategy`,
  the Backtester (no code change needed), and `ExperimentRegistry`'s
  persistence layer. A `Signal` read back in isolation now always says
  which instrument it's about. See `DECISIONS.md`, ADR-0033.
- ✅ **Research transparency**: `ResearchReport.renderer_error` makes a
  `ClaudeNarrativeRenderer` failure observable instead of collapsing
  into the same `rendered_by == "fallback"` state as the normal,
  unconfigured-Claude path. Deterministic findings and the fallback
  guarantee are unchanged. See `DECISIONS.md`, ADR-0034.
- ✅ **Documentation-only**: `PaperBroker.account_state.equity`'s
  not-marked-to-market limitation is now stated explicitly in multiple
  places (module docstring, `account_state`'s own docstring, this file)
  and pinned by a structural test
  (`tests/test_architecture.py::test_paper_broker_has_no_mechanism_to_mark_a_position_to_a_new_price`)
  that fails on purpose if real mark-to-market is ever added without a
  deliberate ADR -- no behavior changed.
- Also reconciled: `src/broker/base.py`'s docstring now names the three
  different reasons a concrete broker's method can raise
  `NotImplementedError` (not yet built vs. structurally impossible given
  the broker's own API vs. genuinely unsupported), so a broker's own
  docstring is read as authoritative rather than assumed. Requirements
  file cleanup (`requirements.txt` mixing direct and transitive/dev
  dependencies) was inspected and deliberately left alone -- tracked as
  pre-v1.0 debt above, not fixed here, since a disruptive dependency
  change didn't belong in a cleanup pass this narrowly scoped.

Confirmed via real `pytest` on the dev machine: see `PROJECT_STATE.md`
for the exact count.

## Sprint 6 -- Research Pipeline & Experiment Integrity ✅ Complete

Connecting the pieces already built, rather than adding another
isolated feature. Target: a researcher can run an experiment
(strategy + symbol + dataset + parameters + risk allocation) and have
it flow signals -> backtest -> risk -> trades -> performance ->
attribution -> experiment record -> research report, with the
experiment reproducible from its stored definition. Not a rewrite --
every existing capability boundary (`ARCHITECTURE.md`) stays exactly
where it is; this sprint is about composition, not redesign.

**Part 1 -- the reproducibility seam ✅ Complete:**

- ✅ **Experiment specification**: `ExperimentSpec`
  (`src/experiments/spec.py`) -- an immutable record of strategy
  name/version, parameters, symbol, interval, dataset identity, and
  risk config, capturing the first links of the long-term chain
  (strategy/version -> parameters -> dataset/version -> ...) without
  building the entire final schema. See `DECISIONS.md`, ADR-0035.
- ✅ **Strategy identity**: `strategy_version()`
  (`src/strategies/identity.py`) hashes a strategy class's own source --
  automatic, not Git-commit hashing or package versioning (both
  explicitly deferred). A new strategy registry
  (`src/strategies/registry.py`) makes reconstruction by name possible;
  `EMACrossStrategy` registers itself as `"ema_cross"`. See ADR-0035.
- ✅ **Dataset identity**: `dataframe_fingerprint()`
  (`src/utils/hashing.py`) hashes the actual candle values a backtest
  ran against -- distinguishes "this exact data" from "symbol + dates",
  without building a full dataset-versioning system. See ADR-0035.
- ✅ **End-to-end contract test**: `tests/test_pipeline_contract.py` --
  one deterministic, fixture-driven test proving Strategy -> Signal ->
  Backtest -> Risk -> Execution -> Trade -> Performance -> Attribution
  -> Experiment Registry -> spec -> reconstruction all compose, and
  that a strategy rebuilt purely from its stored spec reproduces
  identical decisions.
- ✅ **Symbol/execution invariant**: `PaperBroker.submit_signal()` now
  rejects a `symbol` argument that disagrees with `signal.symbol` --
  the exact gap flagged as deferred in ADR-0033/`PROJECT_STATE.md`'s
  Technical Debt, closed before multi-asset trading makes it dangerous
  rather than theoretical. See `DECISIONS.md`, ADR-0036.

**Explicitly not built in Sprint 6** (per the planning review that
scoped this sprint): live trading of any kind (Alpaca/IG/Tiger
connectivity exists, but the research -> decision -> execution chain
isn't trusted with real money yet); ML prediction models; portfolio
optimization; more indicators; sophisticated (stop-based) risk models;
live market data streaming; real mark-to-market; a dashboard; AI
agents; automatic strategy discovery. All either already tracked
elsewhere in this file or explicitly out of scope for now.

**Part 2 -- close-out ✅ Complete:**

- ✅ **Worked example**: `scripts/run_experiment.py` wires an actual
  experiment run (strategy pick, symbol, candles, parameters, risk
  allocation) through the full chain via a single function call
  (`run_experiment(...)`) or a short CLI command (`python
  scripts/run_experiment.py --symbol SPY --strategy ema_cross`).
  Deliberately outside `src/` -- not a permanent orchestration API
  (ADR-0021's deferred `PaperTradingLoop`). Its core function is
  network-free and directly tested against both registered strategies
  (`tests/test_run_experiment_script.py`); only its thin `main()`
  wrapper touches the network. See `DECISIONS.md`, ADR-0037.
- ✅ **Second registered strategy**: `RSIMeanReversionStrategy`
  (`src/strategies/rsi_mean_reversion.py`, registered as
  `"rsi_mean_reversion"`) -- the deliberately opposite trading idea
  from `EMACrossStrategy` (mean reversion, not trend following), proof
  the registry/`ExperimentSpec` seam genuinely accepts a new strategy
  as an extension, not just in theory. A new structural test
  (`tests/test_architecture.py::test_second_strategy_required_no_changes_to_core_pipeline_modules`)
  asserts directly that adding it touched zero lines in
  `src/backtesting`, `src/experiments/registry.py`, `src/attribution`,
  `src/research`, or `src/broker`. See ADR-0037.

Both items that were blocking Sprint 6's close are now resolved.

## Pre-Sprint 7 -- Timeframe-Agnostic Architecture Corrections ✅ Complete

A targeted correction pass, not a redesign, requested ahead of Sprint 7:
confirm the research/strategy architecture supports daily/swing,
intraday, and minute-scale trading using the same conceptual
Strategy/Backtester/ExperimentSpec interfaces, and fix only the
assumptions that genuinely prevented it. Explicitly not building an HFT
platform this round -- no tick feeds, order-book simulation, exchange
co-location, or sub-millisecond execution infrastructure. See
`DECISIONS.md`, ADR-0038.

- ✅ **Reviewed and confirmed already timeframe-agnostic** (no change
  needed): `Signal.timestamp`/`Trade.entry_time`/`exit_time`/
  `ExperimentSpec.dataset_start`/`dataset_end` are all full-precision
  `pd.Timestamp`, never truncated to a calendar date; the
  `Strategy`/`BaseStrategy` contracts never assume one signal per day or
  per candle; `Backtester`'s trade extraction, position series, and
  equity curve already treat candles as an ordered row sequence, not
  "one row = one trading day"; `CacheManager`'s CSV round-trip preserves
  full timestamp precision (empirically verified); `YFinanceProvider`
  never truncates time-of-day. ADR-0006 (timezone consistency, still
  deferred) was left exactly as deferred -- no second, competing
  timezone system was introduced.
- ✅ **Fixed: hidden daily assumption in Sharpe annualization.**
  `src/backtesting/metrics.py`'s `calculate_metrics()`/`sharpe_ratio()`
  annualized every backtest with a hardcoded, unconditional `252`
  regardless of actual bar size -- correct for daily bars, silently
  wrong by orders of magnitude for intraday ones. New
  `infer_periods_per_year()` estimates the annualization factor from the
  data's own median timestamp spacing; daily-bar behavior is unchanged
  (infers to exactly 252), intraday behavior is now
  order-of-magnitude-correct rather than silently wrong. An explicit
  override remains available on both the function and `Backtester`.
- ✅ **Fixed: timeframe as a first-class, typed experiment concept.**
  `ExperimentSpec.interval` was a bare `str` a caller could misspell,
  and `scripts/run_experiment.py`'s `main()` recorded a hardcoded
  `interval="1d"` independently of whatever `MarketDataService`
  actually fetched (which defaults to intraday). `interval` is now a
  typed `Interval` (`src/data/base.py`, the platform's pre-existing
  timeframe enum -- reused, not duplicated); `scripts/run_experiment.py`
  gained a `--interval` CLI flag threaded through to both the fetch and
  the recorded spec, so the two can never silently disagree.
- ✅ **Contract tests**: `tests/test_timeframe_agnostic.py` (6 tests) --
  the identical `EMACrossStrategy` code and pipeline wiring run against
  daily and 1-minute fixtures unmodified; two signals six minutes apart
  within one trading session both survive as a single precisely-timed
  trade (no artificial one-signal-per-session limit); a 5.5-minute
  intraday hold attributes to exactly `pd.Timedelta(minutes=5,
  seconds=30)`, not zero or a date-level bucket; `infer_periods_per_year()`
  returns 252 for daily spacing (unchanged) and two orders of magnitude
  higher for 1-minute spacing; `ExperimentSpec.interval` is confirmed a
  typed `Interval` that round-trips through the registry and rejects an
  unrecognized string.

**Explicitly not built this round:** live intraday or second-scale
trading of any kind; tick feeds; order-book simulation; exchange
co-location; high-frequency execution; sub-millisecond latency
infrastructure; sophisticated market microstructure models; a
session-aware (exchange-hours-precise) annualization refinement for
`infer_periods_per_year()` (it is a calendar-time approximation,
order-of-magnitude-correct for intraday, not exchange-session-precise);
any change to broker interfaces (`src/broker` was reviewed and found to
need none). **This is architectural readiness, not new functionality**
-- no real intraday data has been run through `scripts/run_experiment.py`
against a live provider yet, only through synthetic fixtures.

## Sprint 7 -- Portfolio-Aware Risk & Position Management ✅ Complete

Superseded the originally-planned "Analytics & Dashboard" scope (moved
below, unbuilt, not abandoned) ahead of the sprint starting: the more
pressing requirement was moving the platform from simple per-trade
allocation toward a portfolio-aware risk and position-management
system with explicit loss-based sizing and portfolio constraints,
before either a dashboard or cross-experiment analytics had real
portfolio-level numbers to show. See `DECISIONS.md`, ADR-0039.

- ✅ **Genuine, stop-based position sizing**: `PortfolioRiskEngine.decide()`
  (`src/risk/portfolio_risk.py`) sizes from a defined loss boundary --
  `risk_quantity = floor(equity * risk_pct_per_trade /
  abs(entry_price - stop_price))` -- a hard ceiling, never a target.
  Standalone from `PositionSizer` (unchanged, still allocation-only);
  `RiskLimits.allocation_per_trade_pct` was **not** reverted to a
  risk-based meaning -- Allocation and Risk stay distinct concepts in
  distinct classes (`RiskLimits` vs. the new `PortfolioRiskLimits`).
- ✅ **Long/short risk symmetry**: stop-direction validated explicitly
  (`LONG`: `stop_price < entry_price`; `SHORT`: `stop_price >
  entry_price`), both directions sized through the same formula.
- ✅ **Capital/affordability constraints reduce, never silently clip**:
  capital, allocation, total-exposure, and symbol-exposure ceilings are
  each computed independently and observable on the returned
  `RiskDecision` (`capital_quantity`, `allocation_quantity`,
  `portfolio_exposure_quantity`, `symbol_exposure_quantity`), with
  `limiting_constraint` naming which one(s) bound.
- ✅ **Neutral `Portfolio` domain model** (`src/portfolio/models.py`) --
  cash, positions, per-symbol and total exposure -- reusing
  `AccountState` (via `to_account_state()`) rather than duplicating its
  concept.
- ✅ **Platform-level `Position` model** (`src/portfolio/position.py`) --
  broker-independent, `OPEN`/`CLOSED` lifecycle, stop price,
  realized/unrealized P&L -- deliberately coexisting with (not
  replacing) `src.execution.models.Position`.
- ✅ **Portfolio constraint engine with structured results** --
  `RejectionReason(str, Enum)` (13 members), never a free-form string;
  `RiskDecision` (frozen dataclass) exposes every intermediate quantity
  for full auditability.
- ✅ **Signal/execution symbol consistency** -- `PaperBroker`'s existing
  ADR-0036 enforcement confirmed to still hold with a `RiskDecision` in
  the loop (`tests/test_sprint7_integration.py`).
- ✅ **Connected to the existing execution layer without redesigning
  it** -- `RiskDecision.as_sizing_decision()` adapts into the
  pre-existing `SizingDecision` shape; `src/execution/portfolio_sync.py`'s
  `apply_fill_to_portfolio()` is the small glue keeping `PaperBroker`
  and `Portfolio` in sync, on the allowed side of the
  `src/portfolio`-depends-on-nothing dependency-isolation boundary.
- ✅ **Multi-position support**: three concurrent positions opened and
  coexisting end to end; a same-symbol scaling attempt rejected
  (`POSITION_SCALING_NOT_SUPPORTED`); existing exposure correctly
  constrains a new trade on a different symbol
  (`tests/test_sprint7_integration.py`).
- ✅ **Closing releases exposure, updates capital, records realized
  P&L** -- deterministic, tested via `Portfolio.close_position()` and
  the full pipeline test.
- ✅ **~90+ new tests** spanning sizing, constraints, and integration:
  `tests/test_portfolio_position.py` (27), `tests/test_portfolio_risk.py`
  (36, including all six of the spec's mandatory worked examples and
  the architectural invariant across five scenarios),
  `tests/test_sprint7_integration.py` (6).
- ✅ **Architecture preserved**: strict dependency direction Portfolio
  -> Risk -> Execution -> Broker, confirmed by a new
  `tests/test_architecture.py` check that `src/risk` never imports
  `src/broker`/`src/execution`; strategies still express intent only, a
  `Signal` with direction/confidence -- `PortfolioRiskEngine` is the
  only new place deciding size and portfolio admission.

**Explicitly not built this round** (per the sprint's own instruction):
Kelly criterion sizing, VaR/CVaR, correlation-aware exposure, portfolio
optimization, factor models, volatility targeting, dynamic hedging,
sophisticated margin modeling, market-impact modeling, order-book
simulation, HFT-style execution, real mark-to-market requiring a live
price feed, advanced broker-specific risk handling, ML-based risk
models. See `DECISIONS.md`, ADR-0039 for the full list and reasoning.

**Cleanup pass ✅ Complete** (`DECISIONS.md`, ADR-0040) -- a review of
the implementation above raised two items, addressed without
redesigning anything: (1) the `FLAT`/close path is now an explicit,
symmetric method, `PortfolioRiskEngine.decide_close()`, rather than
`decide()`'s `ValueError` being the only word on it -- a close is
looked up and permitted, never risk-sized, never gated by an exposure
limit; (2) a `SHORT`'s unmodeled capital/margin ceiling is now typed
(`CapitalConstraintModel.NOT_MODELED`) instead of an ambiguous `None`.
The cleanup also formalized the Risk -> Execution handoff
(`RiskDecision.to_trade_intent() -> ApprovedTradeIntent`, enforcing in
code that execution can never be handed more than Risk approved) and
added a static test confirming `src/execution` never imports the
risk-computation symbols. 27 new tests. No partial-close support and
no new `ExecutionResult` wrapper type were added -- both judged out of
this cleanup's narrow scope. Sandbox-confirmed at 497 passed (up from
470), and confirmed via real `pytest` on the dev machine: 499 passed in
1.18s, all green, zero regressions.

## Sprint 8 -- Market Data Integrity & Session Awareness ✅ Complete

Objective: make market data trustworthy and reproducible enough for
daily and minute-level research and trading. See `DECISIONS.md`,
ADR-0041 for the full design and reasoning.

- ✅ **Timezone policy resolved** (`DECISIONS.md`, ADR-0006, deferred
  since Sprint 1; also referenced but left open by ADR-0019 and
  ADR-0038): internal candle timestamps are timezone-aware UTC,
  always. A full migration through `DataProvider`/`MarketDataService`'s
  return contract, confirmed explicitly before implementation rather
  than scoped down to new surfaces only. The actual test-fixture blast
  radius was one file (`tests/test_market_data.py`) -- far smaller than
  the ~15-25-file pre-implementation estimate, since every other
  existing test builds candles directly and never routes through
  `MarketDataService`.
- ✅ **Validation implemented** (`DECISIONS.md`, ADR-0006):
  `src/data/validation.py`'s `validate_candles()` rejects structurally
  invalid data (`StructuralValidationError`) or financially impossible
  records (`FinancialSanityError`) outright; gap detection (session-
  aware) and zero-volume are non-fatal `ValidationReport` entries, not
  raised.
- ✅ **Session/calendar abstraction** (new): `src/calendar/` --
  `TradingCalendar`/`NYSECalendar`, regular US equity session
  (09:30-16:00 America/New_York). Holidays from a hand-rolled,
  rule-based table (not a hardcoded date list, not a calendar-library
  dependency) -- a deliberate trade-off confirmed before
  implementation, given the sprint's own "don't build a full
  exchange-calendar platform" instruction.
- ✅ **Dataset identity hardened**: `src/data/canonical.py`'s
  `canonicalize_candles()` is the single choke point both
  `dataframe_fingerprint()` (`DECISIONS.md`, ADR-0035) and the cache now
  run through -- a cache hit and a fresh fetch of identical data always
  fingerprint identically.
- ✅ **Incremental cache implemented** (`DECISIONS.md`, ADR-0007,
  deferred since Sprint 1): cache re-keyed from `(symbol, interval,
  start, end)` to `(symbol, interval)`, storing the widest span fetched
  so far; a request extending past it fetches only the new tail. A
  request whose start moves earlier falls back to a full refetch,
  deliberately, rather than general interval-merge logic.
- ✅ **Regular-session filtering**: `MarketDataService` gained a
  `session: SessionPolicy` parameter (`REGULAR` default, `ALL`
  opt-out) -- a no-op for daily+ intervals, filters intraday candles to
  the regular session on actual trading days otherwise.
- ✅ **Canonical Dataset**: `get_candles()`/`get_history()` keep their
  exact pre-Sprint-8 signatures; the new `get_dataset()` returns the
  full `CandleDataset`. `Backtester.run()` gained an optional `dataset`
  parameter so a `BacktestResult` can carry a `dataset_identity` --
  both purely additive, no existing call site required a change.

**Explicitly not built this round** (per the sprint's own instruction):
a market-data warehouse, tick or order-book data, corporate-actions
infrastructure, a complete exchange-calendar platform, multiple
commercial providers, real-time streaming, live trading orchestration,
statistical anomaly detection, pre-market/after-hours session support,
general interval-merge caching for a request whose start moves
earlier, or weekly/monthly gap detection. See `DECISIONS.md`, ADR-0041
for the full list and reasoning.

70 new tests (`tests/test_calendar.py`: 19,
`tests/test_data_validation.py`: 24, `tests/test_data_canonical.py`:
13, plus 11 added to `tests/test_market_data.py` and 3 added to
`tests/test_architecture.py`). Sandbox-confirmed at 567 passed (up from
497 sandbox / 499 real after Sprint 7's cleanup).

**Correction:** this section previously claimed the real `pytest` run
on the dev machine returned "569 passed, 0 failed, 0 errors" -- it
actually returned **1 failed, 568 passed**, and that claim was written
before the real run had been reported. The failure
(`test_canonicalize_is_idempotent`, a `.freq`-bookkeeping mismatch
between two canonicalization passes) has since been fixed in
production code (`src/data/canonical.py` now pins `DatetimeIndex.freq`
to `None`), with 3 regression tests added. Sandbox-reverified at **570
passed**, same 2 known environment-only failures. Confirmed via real
`pytest` on the dev machine: **572 passed, 0 failed, 0 errors, all
green** -- both previously sandbox-only artifacts passed for real.
Sprint 8, including this cleanup, is now genuinely verified. See
`DECISIONS.md`, ADR-0041 for the full account.

## Sprint 9 -- Analytics & Dashboard ✅ Complete (confirmed via real pytest: 688 passed, 0 failed)

Originally slated for Sprint 7; re-sequenced twice (not abandoned) --
first when the portfolio-aware risk requirement took priority
(Sprint 7, `DECISIONS.md` ADR-0039), then again when the market-data
integrity requirement took priority (Sprint 8, ADR-0041). Now built,
resolving the Sprint 9 roadmap ambiguity in favor of Candidate A
(Analytics & Dashboard) over Candidate B (AI, moved below as future
work). See `DECISIONS.md`, ADR-0042 for the full design and reasoning.

- ✅ **`src/analytics/`**: deterministic, no-Streamlit-dependency
  backtest performance metrics -- `Metric`/`MetricStatus` (every
  computed number is either a real value or an explicit `UNDEFINED`
  with a reason, never a silently fabricated `0`/`inf`/`nan`);
  `AnalyticsService.analyze_backtest()`/`analyze_experiment()`
  (Sharpe, volatility, max drawdown, win rate, profit factor,
  expectancy, average/largest winner/loser -- the last six in
  return-fraction space, since `Trade` has no persistent share count
  to derive a dollar figure from without inventing one);
  `compare_experiments()` (flags, never blocks, material differences
  in symbol/interval/dataset/strategy-version -- deliberately no
  composite "best strategy" score); `PortfolioValuationService`
  (read-only mark-to-market snapshot of a `Portfolio` -- never
  assigns `Position.current_price`, degrades any position missing a
  price to an explicit `UNDEFINED` aggregate rather than a silently
  partial sum, while still showing the per-position breakdown).
  `ExperimentRegistry` extended to persist trades/equity
  curve/portfolio snapshot/research report per experiment -- the gap
  that made a `PortfolioValuationService`-shaped read possible at all
  for a stored experiment.
- ✅ **`src/dashboard/`**: read-only Streamlit UI over `analytics` --
  Overview, Experiment Analysis, Comparison, and Paper Portfolio
  pages. Never computes its own metrics (everything comes from
  `AnalyticsService`/`PortfolioValuationService`), never bypasses
  `MarketDataService` (the one price touchpoint goes through
  `analytics.valuation.default_price_lookup()`, confirmed by a
  dedicated architecture test after an early draft was caught
  constructing `MarketDataService` directly), and structurally cannot
  submit orders or mutate `Portfolio`/experiment state -- a place to
  see the system running, not a place where new logic lives.
- ✅ **~97 new tests**: `tests/test_analytics.py` (59, hand-calculable
  edge cases + timeframe/annualization + comparison + degraded-legacy-
  experiment cases), `tests/test_portfolio_valuation.py` (13, incl.
  a read-only-invariant test and `default_price_lookup()`'s
  `MarketDataService` construction), `tests/test_dashboard_formatting.py`
  (16, unconditional), `tests/test_dashboard_smoke.py` (9, network-free
  `streamlit.testing.v1.AppTest`, gated with
  `pytest.importorskip("streamlit")` so a machine without Streamlit
  skips cleanly rather than failing), plus 8 added to
  `tests/test_architecture.py` for the new package boundaries and
  extensions to `tests/test_experiments.py`/`test_run_experiment_script.py`
  for the registry persistence additions.

**Explicitly not built this round** (per the sprint's own scope):
real broker P&L wired into the dashboard (paper portfolio only);
a global `PaperTradingLoop` orchestrating live valuation continuously
(the dashboard reads a stored snapshot on demand instead); the
Overview page's experiment list is capped at 20 rather than paginated;
`infer_periods_per_year()`'s calendar-time (not exchange-session-
precise) annualization approximation is inherited unchanged from
ADR-0038, not revisited here.

Sandbox-confirmed at 677 passed, 2 known environment-only failures, 1
correctly-skipped (`test_dashboard_smoke.py`, no Streamlit in the
sandbox). Confirmed via real `pytest` on the dev machine: **688
passed, 0 failed, all green** -- all 9 `test_dashboard_smoke.py` tests
ran for real and passed, and the 2 sandbox-only failures (cli/doctor,
config) also passed for real, confirming they were always
environment-only. Getting to that clean run took three real-`pytest`
rounds, each finding and fixing a genuine issue in
`tests/test_dashboard_smoke.py` itself (never production code) -- see
`DECISIONS.md`, ADR-0042 for the full account, and `PROJECT_STATE.md`
for the current status.

## Sprint 10 -- ML Signal Research & AI Strategy Integration ✅ Complete (confirmed via real pytest: 784 passed, 0 failed)

Sprint 9's deferred Candidate B, now built: classic ML (scikit-learn
`LogisticRegression`) on engineered features, consumed by
`src/strategies` as one signal source among others (`DECISIONS.md`,
ADR-0001) -- not a rewrite of the strategy layer, and not the
LLM-based-reasoning alternative (that remains explicit future work).
See `DECISIONS.md`, ADR-0043 for the full design and reasoning.

- ✅ **`src/ai/`**: `features.py`/`labels.py` (past-only features, a
  separately-configured 3-class forward-return label),
  `dataset.py` (aligned, leakage-free training table),
  `splitting.py` (chronological split with purge/embargo, expanding
  walk-forward evaluation), `model.py` (`AIModel` interface +
  `LogisticRegressionModel`, extensible via `register_model_type()`),
  `identity.py`/`artifacts.py`/`registry.py` (deterministic model
  identity, joblib artifact persistence, JSON metadata registry),
  `evaluation.py` (classification-quality metrics, distinct from
  trading performance), `training.py` (`MLTrainingService`, the one
  application-facing entry point).
- ✅ **`src/strategies/ai_signal.py`**: `AISignalStrategy` -- loads a
  frozen, already-trained model, maps predictions onto the existing
  `Signal`/`SignalDirection`, sparse emission (ADR-0015), model
  provenance in signal metadata. `Backtester`/`src.risk`/
  `src.execution`/`src.portfolio`/`src.dashboard` all remain exactly
  as AI-agnostic as before -- no AI-specific branch anywhere in any of
  them.
- ✅ **~50 new tests**: `tests/test_ai_features.py`,
  `test_ai_labels.py`, `test_ai_dataset.py`, `test_ai_splitting.py`
  (network-free, scikit-learn-free, run in every environment);
  `test_ai_model.py`, `test_ai_registry.py`, `test_ai_training.py`,
  `test_ai_signal_strategy.py`, `test_ai_end_to_end.py` (gated on
  scikit-learn/joblib via `pytest.importorskip`, the same pattern
  Sprint 9 established for Streamlit); 6 new boundary assertions added
  to `tests/test_architecture.py`.

**Explicitly not built this round** (per the sprint's own scope): no
LLM anywhere in the AI signal layer (the AI Research Reporter's
separate LLM/fallback narrative path is untouched); no model zoo
(`LogisticRegression` only, though a second implementation is now
"implement it, register it," not a rewrite); no hyperparameter
optimizer; no automatic "best model" selection; no live inference
daemon, continuous retraining, or autonomous trading; no new
AI-specific dashboard page.

Sandbox-confirmed at 725 passed, 2 known environment-only failures, 8
skipped (scikit-learn/joblib not installed in this sandbox), then
confirmed by real pytest on the dev machine at **784 passed, 0
failed** -- the 2 sandbox-only failures (Python-version check, config)
don't reproduce there since the dev machine runs the actual supported
Python version, and the 8 sandbox-skipped modules ran and passed for
real once scikit-learn/joblib were available. Two test-authoring bugs
the real run surfaced (a Trade-equality comparison that included
randomly-generated Signal UUIDs, and a substring architecture check
that false-positived on `AISignalStrategy`) were fixed in a follow-up
commit -- see `DECISIONS.md`, ADR-0043 for the full account.

## Sprint 11+ -- future work (planned)

- LLM-based reasoning over market context as a second AI signal
  approach, if warranted after Sprint 10's classic-ML baseline is
  evaluated -- `src.ai.model`'s `register_model_type()` seam exists
  specifically so this is additive, not a rewrite.
- An AI-specific dashboard page once multiple trained models/
  experiments exist to compare (Sprint 10 spec explicitly deferred
  this; the existing Overview/Analysis/Comparison/Paper Portfolio
  pages cover today's needs).
- CI running the full suite automatically (see "Ongoing" below).

## Ongoing, not sprint-scoped

- CI running `pytest` (and `python -m src.cli doctor`, now that it
  exists and returns a real exit code) on every push -- not set up yet.
- Documentation set (`PROJECT_STATE.md`, `ARCHITECTURE.md`,
  `CHANGELOG.md`, `DECISIONS.md`, `ROADMAP.md`) updated every sprint.
- **Full experiment lineage** (`src/experiments/`): signals
  (`DECISIONS.md`, ADR-0016) and, as of Sprint 6, one `ExperimentSpec`
  per experiment (ADR-0035) are first-class rows; trades, attribution,
  and research reports still stay in-memory, produced on demand from a
  `BacktestResult`/`AttributionReport` pair. A future evolution could
  link those into the registry too, so one experiment retains a
  complete, reproducible lineage -- strategy/version, parameters,
  dataset/version, signals, trades, metrics, attribution, and a research
  report, all queryable together. Not started this round -- noted here
  so the shape isn't lost, not because it's scheduled.
- **Dataset snapshotting/archival**: Sprint 6's `dataset_fingerprint`
  (ADR-0035) detects when the underlying data behind a dataset
  descriptor has changed, but doesn't store or version the data itself
  -- there's no way to retrieve "the exact candles experiment #42 used"
  after the fact, only to tell whether a fresh fetch matches them. A
  real dataset-versioning system would close this; explicitly deferred
  as "don't build a massive data-versioning system yet" in the Sprint 6
  planning review.
- **Strategy source archival**: similarly, `strategy_version` (ADR-0035)
  hashes a strategy's source but doesn't store the source itself -- if
  `EMACrossStrategy` changes later, an old experiment's
  `verify_strategy_version()` will correctly report `False`, but there's
  no way to recover exactly what the old implementation was from the
  spec alone (only from version control, which this ADR deliberately
  does not integrate with yet).
- **`PositionSizer`/`Backtester` wiring**: `Backtester` still sizes every
  trade as a single unit (ADR-0011); `PositionSizer` and `PaperBroker`
  compose correctly (`tests/test_integration_paper_trading.py`,
  `tests/test_pipeline_contract.py`) but only as a second, parallel path
  driven by the same signals, not as part of `Backtester.run()` itself.
  Wiring these together is real future work, deliberately not done in
  Sprint 6 (out of scope: "sophisticated risk models").
- **Session-aware Sharpe annualization**: `infer_periods_per_year()`
  (Pre-Sprint 7, `DECISIONS.md` ADR-0038) is a calendar-time
  approximation -- it doesn't know NYSE trading hours, market holidays,
  or that some markets trade 24/7, so intraday annualization is
  order-of-magnitude-correct, not exchange-session-precise. A future,
  more precise version is real future work, not required to stop
  annualization from being silently wrong the way the old flat `252`
  default was for non-daily bars.
- **Advanced execution/data layer for second-scale strategies**: the
  product requirement behind ADR-0038 explicitly names potential future
  second-scale strategies, gated on "a future advanced execution/data
  layer" -- tick feeds, order-book simulation, exchange co-location,
  high-frequency execution, and sub-millisecond latency infrastructure
  are all explicitly not built and not scheduled. ADR-0038's job was
  only to confirm the research/strategy architecture doesn't
  architecturally prevent that layer from being added later, not to
  build it.
- **Real intraday data exercised end to end**: `tests/test_timeframe_agnostic.py`
  proves the pipeline against synthetic daily/1-minute fixtures;
  `scripts/run_experiment.py --interval 1m` has not yet been run against
  a live provider. Worth doing once there's an actual intraday research
  question to answer, not scheduled on its own.
- **Sprint 7's explicitly deferred risk capabilities** (`DECISIONS.md`,
  ADR-0039): Kelly criterion sizing, VaR/CVaR, correlation-aware
  exposure, portfolio optimization, factor models, volatility
  targeting, dynamic hedging, sophisticated margin modeling,
  market-impact modeling, order-book simulation, real mark-to-market
  for `Portfolio`/`Position` (both fall back to `entry_price` exactly
  like `PaperBroker.account_state` already does), and ML-based risk
  models. None scheduled; tracked so the scope boundary isn't lost.
- **`PortfolioRiskEngine`/`PaperBroker` position scaling**: neither
  supports adding to or partially reducing an existing position --
  `PortfolioRiskEngine.decide()` rejects a same-symbol call outright
  (`POSITION_SCALING_NOT_SUPPORTED`) rather than attempting it. Real
  future work, gated on `PaperBroker` itself gaining that capability
  first (`DECISIONS.md`, ADR-0022, ADR-0039).
- **`PortfolioRiskEngine` wired into `Backtester`**: like
  `PositionSizer` before it, Sprint 7's engine composes end to end with
  `PaperBroker`/`Portfolio` only via
  `tests/test_sprint7_integration.py` -- `Backtester.run()` itself still
  sizes every trade as a single unit (ADR-0011). Same deferred wiring
  gap as `PositionSizer`'s, now shared by both risk engines.
