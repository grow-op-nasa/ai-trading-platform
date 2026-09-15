# Project State

_Last updated: 2026-09-15 -- **Sprint 6 (Research Pipeline & Experiment
Integrity) is complete.** Part 2 closed both items left open at the end
of part 1: `RSIMeanReversionStrategy` (`src/strategies/rsi_mean_reversion.py`,
registered as `"rsi_mean_reversion"`) is the platform's second permanent
strategy -- a deliberately different trading idea (mean reversion, not
trend following) proving the registry/`ExperimentSpec` seam genuinely
generalizes, not just accommodates the one strategy it was designed
against. `scripts/run_experiment.py` is a worked example wiring one real
experiment through the entire pipeline (Strategy -> Backtest -> Risk ->
Execution -> Attribution -> Research Report -> Experiment Registry), with
its core `run_experiment()` function kept network-free and directly
testable, separate from its thin CLI wrapper. A new structural test
(`tests/test_architecture.py::test_second_strategy_required_no_changes_to_core_pipeline_modules`)
asserts directly -- not just by docstring claim -- that adding the second
strategy touched zero lines in `src/backtesting`, `src/experiments/registry.py`,
`src/attribution`, `src/research`, or `src/broker`. See `DECISIONS.md`,
ADR-0037; `CHANGELOG.md`, "Sprint 6 (complete) -- part 2, close-out."
Confirmed via real `pytest` on the dev machine (Python 3.14.6): **386
passed in 0.92s**, all green._

_Sprint 5 is complete, and a targeted
architecture-review cleanup pass is complete ahead of Sprint 6 (see
`DECISIONS.md`, ADR-0031 through ADR-0034; `CHANGELOG.md`,
"Pre-Sprint 6 -- Architecture Review Cleanup"). Four concrete brokers
implement `BrokerConnection`: **`AlpacaBroker`** (full lifecycle --
`get_account`/`submit_order`/`get_order`/`cancel_order`; real
`ALPACA_API_KEY`/`ALPACA_API_SECRET` are configured and `get_account()`
is confirmed against Alpaca's real paper account), **`IBKRBroker`**
(`get_account()` only; IB's Client Portal Web API -- **confirmed
geo-restricted for this deployment**, OFAC/Section 311, so it remains
an architecture proof without a usable real account), **`IGBroker`**
(IG's REST API, session-token auth -- a real, usable second broker for
this platform's user, since IG doesn't geo-block; `get_account()` plus
`submit_order()`, which resolves synchronously into a final
`FILLED`/`REJECTED` `BrokerOrder` -- `get_order()`/`cancel_order()`
stay `NotImplementedError` since IG has no live status endpoint to poll
and nothing left to cancel once a market order resolves), and
**`TigerBroker`** (Tiger Trade, wraps the official `tigeropen` SDK
rather than hand-rolled RSA request signing -- a real, usable third
broker for this platform's user; `get_account()` only this round,
`submit_order`/`get_order`/`cancel_order` all `NotImplementedError`).
`src/reconciliation/` compares `PaperBroker`'s simulated fills against
real broker fills (`reconcile_fill()`), deliberately depending on both
`src/execution` and `src/broker`. Confirmed via real `pytest` on the
dev machine through Sprint 5: connectivity 213/213, order submission
228/228, order cancellation 233/233, `IBKRBroker` 248/248, fill
reconciliation + `IGBroker` connectivity together at 282/282, IG order
submission at 290/290, and `TigerBroker` at 305/305. Sprints 3, 4 and 5
are all complete and confirmed.

The cleanup that followed corrected four things without redesigning
anything: `AccountState` moved out of `src/risk` into a new, neutral
`src/portfolio` package so `broker` no longer depends on `risk`
(ADR-0031); `RiskLimits.risk_per_trade_pct` was renamed to
`allocation_per_trade_pct` since it was never risk-per-trade in the
stop-loss sense (ADR-0032); `Signal` gained a required, first-class
`symbol` field, threaded through strategies, the Backtester (no code
change needed there), and the Experiment Registry (ADR-0033);
`ResearchReport` gained `renderer_error` so a failed
`ClaudeNarrativeRenderer` call is now observable rather than silently
swallowed (ADR-0034); and `PaperBroker`'s not-marked-to-market
limitation is now stated explicitly in multiple docstrings and pinned
by a structural test. Confirmed via real `pytest` on the dev machine
(Python 3.14.6): **329 passed**, all green.

**Sprint 6 (Research Pipeline & Experiment Integrity) is in progress.**
Part 1 is built: `ExperimentSpec` (`src/experiments/spec.py`) captures
an immutable, reproducible record of what an experiment actually was --
strategy name/version, parameters, dataset identity, risk config
(`DECISIONS.md`, ADR-0035); strategy version is a hash of the
strategy's own source (`src/strategies/identity.py`), dataset identity
is a content hash of the actual candles used (`src/utils/hashing.py`);
a new strategy registry (`src/strategies/registry.py`) makes
`ExperimentSpec.reconstruct_strategy()` possible; `ExperimentRegistry`
gained `save_spec()`/`get_spec()`; and `PaperBroker.submit_signal()` now
rejects a `symbol`/`signal.symbol` mismatch (`DECISIONS.md`, ADR-0036).
A new end-to-end contract test
(`tests/test_pipeline_contract.py`) proves the whole chain -- Strategy
-> Signal -> Backtest -> Risk -> Execution -> Trade -> Performance ->
Attribution -> Experiment Registry -> spec -> reconstruction --
composes and reproduces identical decisions. Confirmed via real
`pytest` on the dev machine (Python 3.14.6): **364 passed**, all
green._

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
  Keys are real, live checks; Broker Connection is now real too, gated
  on configuration (`NOT_IMPLEMENTED` until `ALPACA_API_KEY`/
  `ALPACA_API_SECRET` are set, then a live `OK`/`FAIL`). Run via
  `python -m src.cli doctor`. See `DECISIONS.md`, ADR-0013 (original
  design), ADR-0020 (API Keys upgraded), and ADR-0023 (Broker
  Connection upgraded once `src/broker` existed to check).
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
  (`RiskLimits.allocation_per_trade_pct`, default 10% -- renamed from
  `risk_per_trade_pct` by ADR-0032; this is capital allocation, not a
  maximum-loss guarantee), the same for every signal regardless of
  `Signal.confidence`. Sizes down to remaining portfolio exposure
  headroom (`RiskLimits.max_portfolio_exposure_pct`, default 50% of
  equity) rather than rejecting outright when the full allocation
  doesn't fit; only rejects when there's no headroom left. Deliberately
  standalone from `Backtester` this round -- ADR-0011's single-unit
  execution model is untouched. Touches zero existing files (Extension
  Cost: 0). See `DECISIONS.md`, ADR-0021.
- ✅ Paper Execution (`src/execution/`) -- Sprint 4.
  `PaperBroker.submit_signal(signal, symbol, fill_price,
  sizing_decision=None)` translates a signal into an `Order`, fills it
  instantly and completely at the given price (no slippage/commission),
  and tracks cash + one open position per symbol. `account_state`
  returns a real `src.portfolio.AccountState` (moved from `src.risk` by
  ADR-0031) -- cash plus each position's signed value at entry price
  for `equity`, unsigned cost basis for `open_exposure` -- closing the
  loop `PositionSizer` left open. Not marked to market: equity reflects
  entry-price valuation until a position closes and P&L realizes into
  cash -- this limitation is now stated explicitly in multiple
  docstrings and pinned by a structural test
  (`tests/test_architecture.py`). Touches zero existing files
  (Extension Cost: 0). See `DECISIONS.md`, ADR-0022.
- ✅ End-to-end proof (`tests/test_integration_paper_trading.py`) --
  Sprint 4. `EMACrossStrategy`'s real signals over real candles, fed
  through `PositionSizer` then `PaperBroker` exactly as a future
  live/paper trading loop would. No new production code. Confirms a
  signal sized after a completed round trip uses the account's updated
  post-trade equity, not the original starting value -- proving the
  `PositionSizer` <-> `PaperBroker` loop (ADR-0021/ADR-0022) actually
  closes when driven by a real strategy, not just constructed test
  objects.
- ✅ Broker Connectivity (`src/broker/`) -- Sprint 5.
  `BrokerConnection` (analogous to `DataProvider`) has one method,
  `get_account() -> src.portfolio.AccountState` (moved from `src.risk`
  by ADR-0031, so `src/broker` no longer needs to import `src/risk` at
  all) -- reusing the platform's existing account-state currency rather
  than a parallel model.
  `AlpacaBroker` reads `ALPACA_API_KEY`/`ALPACA_API_SECRET` from
  arguments or the environment, raises immediately if either is
  missing, defaults to Alpaca's **paper** endpoint (never live, without
  an explicit override), and maps 401/403 to `BrokerAuthenticationError`
  and any other failure to `BrokerConnectionError`. Built and tested
  against a fake HTTP session -- no real Alpaca account exists yet.
  Touches 1 existing file (`src/cli/checks.py`, Extension Cost: 1). See
  `DECISIONS.md`, ADR-0023.
- ✅ Order Submission (`src/broker/`) -- Sprint 5.
  `OrderRequest`/`BrokerOrder`/`OrderStatus` (`src/broker/models.py`),
  deliberately independent of `src.execution.models` to avoid a
  backwards dependency (`ARCHITECTURE.md` draws `execution --> broker`,
  not the reverse). `BrokerConnection.submit_order()`/`get_order()`
  round out the interface; `AlpacaBroker` implements both against
  `POST /v2/orders` / `GET /v2/orders/{id}`, sharing a new `_request()`
  helper with `get_account()`. Alpaca's raw status strings map onto
  `OrderStatus` via an explicit table; an unrecognized status raises
  rather than guessing. Market orders only, submit + status check only
  -- no cancellation this round. Extension Cost: 0 files changed outside
  `src/broker/`. See `DECISIONS.md`, ADR-0024.
- ✅ Order Cancellation (`src/broker/`) -- Sprint 5.
  `BrokerConnection.cancel_order(broker_order_id) -> None` rounds out
  order management to submit + status + cancel. `AlpacaBroker` calls
  `DELETE /v2/orders/{id}` and returns `None` on success -- confirms
  only that the broker accepted the cancellation, not that the order
  actually ended up canceled, since real cancellation is asynchronous
  (the order may already have filled). Callers wanting the actual
  outcome call `get_order()` afterward. Extension Cost: 0 files changed
  outside `src/broker/`. See `DECISIONS.md`, ADR-0025.
- ✅ Second Broker -- Interactive Brokers (`src/broker/ibkr.py`) --
  Sprint 5. `IBKRBroker(BrokerConnection)` is the platform's second
  concrete broker -- proof the interface is actually swappable, built
  against IB's Client Portal Web API (REST-based, same injectable
  `_Session` pattern as `AlpacaBroker`) rather than the socket-based TWS
  API. Takes no credential arguments -- IB's retail auth is a browser
  session against a locally running Client Portal Gateway, not a simple
  key/secret pair. No separate paper/live endpoint (unlike Alpaca) --
  determined by which account is logged into the gateway.
  `get_account()` resolves the account via `GET /iserver/accounts` then
  reads `netliquidation`/`grosspositionvalue` from `GET /portfolio/
  {accountId}/summary`. `submit_order`/`get_order`/`cancel_order` all
  raise `NotImplementedError` this round -- IB's order flow (conid
  lookup, reply/confirmation handling) needs its own design pass.
  Extension Cost: 1 file changed outside the new `ibkr.py`/
  `test_ibkr.py` (`src/broker/__init__.py`, exports). See
  `DECISIONS.md`, ADR-0026. **Note:** IB geo-restricts account access
  for this deployment (OFAC/Section 311 special measures) -- confirmed
  inaccessible for real use. `IBKRBroker` remains valid as the
  architecture proof it was built for, but won't get further real-world
  investment (e.g. IB order submission) unless that changes.
- ✅ Fill Reconciliation (`src/reconciliation/`, new package) -- Sprint
  5. `reconcile_fill(real_order: BrokerOrder, simulated_fill: Fill) ->
  FillReconciliation` compares what `PaperBroker` assumes (instant,
  complete fill at a caller-supplied price) against what a real broker
  order actually did. All price/cost fields are side-normalized --
  positive always means real execution was worse than simulated,
  regardless of BUY/SELL. Handles partial fills via
  `quantity_shortfall`; `cost_impact` uses the real filled quantity.
  Deliberately depends on both `src/execution` and `src/broker` -- a
  documented exception to ADR-0024's independence rule, since this is a
  comparison layer neither capability depends back on (same shape as
  `src/attribution` depending on `src/backtesting` + `src/regime`).
  Single-order primitive only this round. Extension Cost: 0 files
  changed outside the new package. See `DECISIONS.md`, ADR-0027.
- ✅ Third Broker -- IG (`src/broker/ig.py`) -- Sprint 5. `IGBroker` is
  the platform's third concrete broker, and the first second-broker
  candidate the user can actually use with a real account (unlike
  `IBKRBroker`, IG doesn't geo-block). Session-based auth: API key +
  username + password (`IG_API_KEY`/`IG_USERNAME`/`IG_PASSWORD`) -> a
  `POST /session` login -> cached `CST`/`X-SECURITY-TOKEN` tokens (no
  refresh logic this round). `IG_DEMO_BASE_URL`/`IG_LIVE_BASE_URL`
  select environment explicitly, like Alpaca. `get_account()` resolves
  the account (explicit, "preferred," or first-listed) and maps
  `balance.balance` -> equity, `balance.deposit` (margin committed) ->
  open exposure -- a documented approximation for CFD/spread-bet
  accounts. `submit_order`/`get_order`/`cancel_order` raise
  `NotImplementedError` -- IG's deal-reference/confirm order model
  doesn't fit `BrokerOrder`/`OrderStatus` without its own design round.
  Extension Cost: 1 file changed outside the new `ig.py`/`test_ig.py`
  (`src/broker/__init__.py`, exports). See `DECISIONS.md`, ADR-0028.
- ✅ IG Order Submission (`src/broker/ig.py`) -- Sprint 5.
  `IGBroker.submit_order()` implemented: `POST /positions/otc` then
  `GET /confirms/{dealReference}`, resolving **synchronously** into a
  final `BrokerOrder` (`FILLED` on `ACCEPTED`, `REJECTED` on
  `REJECTED`; an unrecognized status raises `BrokerConnectionError`).
  Order `currencyCode` is read from the selected account's own
  `currency` field via a new shared `_get_selected_account()` helper --
  never guessed or hardcoded. `get_order()`/`cancel_order()` stay
  `NotImplementedError`: IG has no live endpoint to check status on a
  resolved market order, and nothing left to cancel by the time
  `submit_order()` returns. Extension Cost: 0 files changed outside
  `ig.py`/`test_ig.py`. See `DECISIONS.md`, ADR-0029.
- ✅ Fourth Broker -- Tiger Trade (`src/broker/tiger.py`) -- Sprint 5.
  `TigerBroker` is the platform's fourth concrete broker, and the first
  built by wrapping an official vendor SDK (`tigeropen`) instead of
  talking `requests` directly -- Tiger's auth requires RSA-signing every
  request (PKCS#1 private key), too risky to hand-roll. The injectable
  seam is the SDK's `TradeClient` object, not an HTTP session.
  `tigeropen` is imported lazily and deliberately not added to
  `requirements.txt` (mirrors ADR-0020's `anthropic` precedent).
  Credentials (`tiger_id`/`private_key_path`/`account`, env-var fallback
  `TIGER_ID`/`TIGER_PRIVATE_KEY_PATH`/`TIGER_ACCOUNT`) are a fourth
  distinct auth shape. No separate paper/live URL -- like `IBKRBroker`,
  the `account` value itself decides. `get_account()` maps
  `summary.net_liquidation` -> equity, `summary.gross_position_value`
  (defaulting to `0.0`) -> open exposure -- the most direct account
  mapping of any broker so far -- and wraps any client exception into
  `BrokerConnectionError` (a documented, provisional limitation).
  `submit_order`/`get_order`/`cancel_order` all raise
  `NotImplementedError` -- connectivity and account state only this
  round. Extension Cost: 1 file changed outside the new `tiger.py`/
  `test_tiger.py` (`src/broker/__init__.py`, exports + docstring). See
  `DECISIONS.md`, ADR-0030.
- ✅ Pre-Sprint 6 architecture review cleanup -- a targeted correction
  pass, not a redesign, requested after Sprint 5 landed. `src/portfolio/`
  (new) holds the neutral `AccountState`, moved out of `src/risk` so
  `broker --> portfolio`, `risk --> portfolio`, `execution -->
  portfolio` and `broker` never depends on `risk` (ADR-0031).
  `RiskLimits.risk_per_trade_pct` renamed to `allocation_per_trade_pct`
  everywhere, with no change to the underlying math -- it was always a
  fixed allocation, never a stop-based risk model (ADR-0032). `Signal`
  gained a required, first-class `symbol` field, threaded through
  `BaseStrategy`/`EMACrossStrategy` and the Experiment Registry's
  `signals` table; the Backtester needed no code change (ADR-0033).
  `ResearchReport` gained `renderer_error: str | None`, so a
  `ClaudeNarrativeRenderer` failure (bad creds, network error, malformed
  response) is now observable instead of silently indistinguishable
  from "no API key configured" (ADR-0034). `PaperBroker`'s
  not-marked-to-market limitation is documented more explicitly across
  `src/execution` and pinned by a new structural test rather than left
  as a single roadmap line. New `tests/test_architecture.py` (8 tests)
  and `tests/test_portfolio.py` (5 tests) protect the corrected shape
  directly. `requirements.txt`'s direct/transitive mixing was inspected
  and deliberately left alone (documented as pre-v1.0 debt). No new
  feature surface, no interface redesign (`BrokerConnection`'s
  `NotImplementedError` gaps are untouched, now with a documented
  three-way taxonomy in `src/broker/base.py`), no Experiment Registry
  scope expansion. Confirmed via real `pytest` on the dev machine
  (Python 3.14.6): 329 passed, all green. See `DECISIONS.md`, ADR-0031
  through ADR-0034; `CHANGELOG.md`, "Pre-Sprint 6 -- Architecture
  Review Cleanup."
- ✅ Sprint 6 part 2 -- Research Pipeline & Experiment Integrity
  close-out (`DECISIONS.md`, ADR-0037). `RSIMeanReversionStrategy`
  (`src/strategies/rsi_mean_reversion.py`), registered as
  `"rsi_mean_reversion"`, is the platform's second permanent
  strategy -- deliberately the opposite trading idea from
  `EMACrossStrategy` (mean reversion, not trend following): long-only,
  enters `LONG` at or below an `oversold` RSI threshold (default 30),
  exits to `FLAT` at or above an `overbought` threshold (default 70).
  `scripts/run_experiment.py` (+ `scripts/__init__.py`) is a worked
  example wiring one real experiment through Strategy -> Backtest ->
  Risk -> Execution -> Attribution -> Research Report -> Experiment
  Registry; its core `run_experiment()` function is network-free and
  directly testable, kept separate from a thin argparse `main()` that
  is the only code path touching `MarketDataService`. Deliberately
  placed in the pre-existing top-level `scripts/` directory, not
  `src/`, to avoid committing to a permanent orchestration API this
  round (ADR-0021's deferred `PaperTradingLoop`). A new structural test
  (`tests/test_architecture.py::test_second_strategy_required_no_changes_to_core_pipeline_modules`)
  greps `src/backtesting`, `src/experiments/registry.py`,
  `src/attribution`, `src/research`, and `src/broker` for any mention
  of the new strategy and fails if it finds one -- direct proof, not a
  docstring claim, that the second strategy was an extension, not a
  rewrite. Extension Cost: 2 files touched outside new files
  (`src/strategies/__init__.py`, `DECISIONS.md`). Both of Sprint 6's
  close-out items are resolved; **Sprint 6 is complete.**
- ✅ Sprint 6 part 1 -- Research Pipeline & Experiment Integrity: the
  reproducibility seam. `ExperimentSpec` (`src/experiments/spec.py`) is
  an immutable record of strategy name/version, parameters, symbol,
  interval, dataset identity, and risk config -- the first links of the
  platform's long-term chain (strategy/version -> parameters ->
  dataset/version -> signals -> trades -> metrics -> attribution ->
  report), not the entire schema. Strategy version
  (`src/strategies/identity.py`) is a SHA-256 hash of a strategy
  class's own source -- automatic, not Git-commit hashing or a
  manually maintained version string. Dataset identity
  (`src/utils/hashing.py`'s `dataframe_fingerprint()`) is a content
  hash of the actual candles used -- distinguishes "this exact data"
  from "symbol + dates" without a full versioning system. A new
  strategy registry (`src/strategies/registry.py`,
  `EMACrossStrategy` registered as `"ema_cross"`) makes
  `ExperimentSpec.reconstruct_strategy()` possible;
  `verify_strategy_version()`/`verify_dataset()` detect drift.
  `ExperimentRegistry` gained `save_spec()`/`get_spec()`, additive and
  separate from `log_experiment()` (same reasoning as `save_signals()`,
  ADR-0016). `PaperBroker.submit_signal()` now rejects a
  `symbol`/`signal.symbol` mismatch -- the gap flagged as deferred in
  ADR-0033, closed before it becomes dangerous under multi-asset
  trading. One new end-to-end contract test
  (`tests/test_pipeline_contract.py`) proves the whole chain -- Strategy
  -> Signal -> Backtest -> Risk -> Execution -> Trade -> Performance ->
  Attribution -> Experiment Registry -> spec -> reconstruction --
  composes and reproduces identical decisions from a strategy rebuilt
  purely from its stored definition. See `DECISIONS.md`, ADR-0035,
  ADR-0036.

## Current Module

**Sprints 3, 4 and 5 are complete and confirmed, and the Pre-Sprint 6
architecture review cleanup is complete and confirmed.** `src/broker/`
connectivity, order submission, and order cancellation for Alpaca,
`IBKRBroker`, `src/reconciliation/`, `IGBroker` (connectivity +
synchronous order submission), and `TigerBroker` (connectivity +
account state) were all confirmed at 305/305 via real `pytest` on the
dev machine (Python 3.14.6) before the cleanup began. The cleanup
itself is likewise confirmed via real `pytest` on the dev machine:
329 passed in 1.26s, all green.

**Sprint 6 (Research Pipeline & Experiment Integrity) is complete.**
Part 1 -- `ExperimentSpec`, strategy identity/registry, dataset
fingerprinting, `ExperimentRegistry.save_spec()`/`get_spec()`, the
`PaperBroker` symbol invariant, and the end-to-end pipeline contract
test -- was confirmed via real `pytest` on the dev machine (Python
3.14.6): 364 passed in 0.87s. Part 2 -- `RSIMeanReversionStrategy` (a
second, deliberately different registered strategy) and
`scripts/run_experiment.py` (a worked example wiring one real
experiment through the full pipeline) -- closes both items `ROADMAP.md`
listed as blocking the sprint's close. Confirmed via real `pytest` on
the dev machine (Python 3.14.6): **386 passed in 0.92s**, all green.

What's left on the Market Data Service (moved to Roadmap, not
blocking Sprint 2 through 5, or the cleanup): no data validation beyond
required-column checks (ADR-0006); caching is CSV-only and re-fetches
whole ranges on any cache-key miss (ADR-0007); no integration test
suite against the live yfinance API.

## Next Task

Sprint 6 is closed and confirmed (386 passed via real `pytest` on the
dev machine). Begin Sprint 7 (Analytics & Dashboard). Separately
available, none yet explicitly requested: setting
`TIGER_ID`/`TIGER_PRIVATE_KEY_PATH`/`TIGER_ACCOUNT` to exercise
`TigerBroker.get_account()` against a real Tiger paper account; setting
`IG_API_KEY`/`IG_USERNAME`/`IG_PASSWORD` to exercise `IGBroker` against
a real IG account (`get_account()` and, with a real epic symbol,
`submit_order()`); and exercising Alpaca's `submit_order`/`get_order`/
`cancel_order` against the real paper account, then feeding the
resulting `BrokerOrder` and a `PaperBroker`-simulated `Fill` into
`reconcile_fill()` for the first live reconciliation.

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
- `PositionSizer` and `PaperBroker` are proven to compose correctly end
  to end (`tests/test_integration_paper_trading.py`), but neither is
  wired into `Backtester` itself -- `Backtester` still assumes
  single-unit sizing (ADR-0011), unchanged. No reusable orchestration
  layer (e.g. a `PaperTradingLoop`) exists yet either -- the
  integration test wires strategy -> sizer -> broker inline, since
  there's no second real caller yet to justify a new abstraction's
  shape. Confidence-scaled sizing and a position-count-based portfolio
  limit are deferred, not rejected -- see `DECISIONS.md`, ADR-0021.
  `allocation_per_trade_pct` (renamed from `risk_per_trade_pct` by
  ADR-0032) sizes a fixed fraction of equity regardless of stop
  distance -- true risk-based (stop-loss-distance) sizing is a distinct,
  unbuilt capability, not just a naming fix; deliberately not invented
  this round.
- `PaperBroker` doesn't mark positions to market -- `equity` between
  fills can understate or overstate the account's true value whenever
  an open position has moved in price. No live price feed exists for
  it to mark against yet; real broker connectivity (Sprint 5) is the
  natural point to revisit this. Now documented more explicitly across
  `src/execution`'s docstrings and pinned by a structural test
  (`tests/test_architecture.py::test_paper_broker_has_no_mechanism_to_mark_a_position_to_a_new_price`)
  so a future change adding real mark-to-market can't do so silently.
  See `DECISIONS.md`, ADR-0022, reaffirmed by ADR-0031's cleanup.
- `PaperBroker` supports only one open position per symbol at a time --
  opening a second raises rather than averaging/scaling into it. Also
  deferred: limit orders, partial fills, slippage, commission.
  `PaperBroker.submit_signal()`'s own `symbol` argument disagreeing with
  the `Signal.symbol` it's handed is now rejected with `ValueError`
  (Sprint 6, `DECISIONS.md` ADR-0036) -- no longer open debt. `Trade`
  (`src/backtesting/models.py`) still has no `symbol` field of its own
  -- it traces back to symbol-carrying `Signal`s via
  `entry_signal_id`/`exit_signal_id`, which was judged sufficient rather
  than duplicative.
- Sprint 6's identity seams (`DECISIONS.md`, ADR-0035) are deliberately
  partial: `strategy_version` hashes a strategy's source but doesn't
  archive the source itself, and `dataset_fingerprint` detects when
  data has changed but doesn't snapshot or store prior versions of it.
  Both are the seam, not the system -- see `ROADMAP.md`'s
  "Ongoing, not sprint-scoped" section.
- `AlpacaBroker.get_account()` is now confirmed against Alpaca's real
  paper-trading API (`atp doctor`'s Broker Connection check passes with
  real `ALPACA_API_KEY`/`ALPACA_API_SECRET` credentials) -- the first
  real-world confirmation that `_parse_account()`'s assumptions about
  Alpaca's response shape hold. `submit_order()`/`get_order()`/
  `cancel_order()` remain untested against the real API -- only against
  a fake session -- since exercising them for real means actually
  placing/canceling a paper order, not just reading account state.
  `IBKRBroker` is likewise untested against a real Client Portal
  Gateway, and only implements `get_account()` -- `submit_order`/
  `get_order`/`cancel_order` raise `NotImplementedError` until IB's
  order flow (conid lookup, reply/confirmation handling) gets its own
  design pass. Session freshness for IB (the gateway's session needs
  periodic "tickle" calls and re-authentication) isn't modeled at all
  yet. Reconciling `PaperBroker`'s simulated fills against a real
  broker's actual fills remains deferred, not rejected -- see
  `DECISIONS.md`, ADR-0023, ADR-0024, ADR-0025, ADR-0026.
- Package layout (`src/` vs. `src/ai_trading_platform/`) and flat
  config constants vs. a typed `Settings` object -- both deferred to
  pre-1.0, tracked as ADR-0004 and ADR-0005.
- `requirements.txt` is a full environment freeze (`pip freeze`), not a
  curated list of direct dependencies -- inspected during the
  Pre-Sprint 6 cleanup and deliberately left unchanged, since there's
  no low-risk way to separate direct from transitive dependencies
  without an actual migration. Tracked as pre-v1.0 debt in `ROADMAP.md`
  alongside ADR-0004.
- `ExperimentRegistry` stores `Signal`s but not yet a full reproducible
  experiment lineage (strategy name/version, parameters, dataset
  version, trades, metrics, attribution, research report all linked
  together) -- scope deliberately held stable during the Pre-Sprint 6
  cleanup rather than expanded speculatively. Noted as future evolution
  in `src/experiments/registry.py`'s docstring and `ROADMAP.md`.

## How to verify this file is accurate

```bash
pytest                    # should show 386 passed (7 config + 15 market data + 6 cache
                          # + 11 indicators + 10 regime + 12 backtesting + 21 experiments
                          # + 29 cli/doctor + 13 signals + 13 strategy_sdk + 8 attribution
                          # + 17 research + 11 ema_cross_strategy + 17 risk + 19 execution
                          # + 6 integration_paper_trading + 34 broker + 15 ibkr
                          # + 11 reconciliation + 31 ig + 15 tiger + 9 architecture
                          # + 5 portfolio + 6 hashing + 9 strategy_registry
                          # + 12 experiment_spec + 3 pipeline_contract
                          # + 14 rsi_mean_reversion_strategy + 7 run_experiment_script)
python src/main.py        # should log startup + watchlist
python -m src.cli doctor  # should print one line per check and end with "Everything Healthy"
                          # (Broker Connection shows NOT_IMPLEMENTED until
                          # ALPACA_API_KEY/ALPACA_API_SECRET are set)
```

Confirmed via real `pytest` on the dev machine (Python 3.14.6): 386
passed in 0.92s, all green.
