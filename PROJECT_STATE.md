# Project State

_Last updated: 2026-09-24 -- **Sprint 13 (AI Research Agent) is
implemented and sandbox-confirmed (network-free, `FakeLLMProvider`
only): 897 passed, 2 known environment-only failures, 13 skipped.**
Real `pytest` on the dev machine, and a manual bounded real-provider
smoke test (`ANTHROPIC_API_KEY` required), are the operator's next step
-- see "Next Task" below for exact commands. The platform's first
genuine agentic loop now exists: `src.ai.agents.agent.ResearchAgent`
takes a research goal, reasons over existing platform evidence
(experiments, analytics, strategies, trained models) via a small,
policy-gated tool surface, can run bounded historical backtests through
a new `src.research.trial_service.ResearchTrialService`, and produces a
structured, evidence-grounded `AgentResearchReport` -- distinguishing
observation from hypothesis, never a trading recommendation. It is
strictly research-only: no tool exists for trading, portfolio/risk
mutation, model/strategy mutation, filesystem/shell access, or web
browsing anywhere in `src/ai/agents` (`DECISIONS.md`, ADR-0046).
`src.research.ResearchReporter` and `src.strategies.ai_signal.
AISignalStrategy` are both completely untouched.

_Previously: Sprint 12 (Execution Realism &
Transaction Cost Modeling) is implemented and confirmed via real
`pytest` on the dev machine: 916 passed, 0 failed (3.92s, no skips --
every scikit-learn/joblib/Streamlit-gated test the sandbox could only
skip ran for real here, and both of the sandbox's own 855/2/8 "known
environment-only" failures did not reproduce).** The
portfolio-aware backtest path (`run_portfolio()`) no longer assumes an
instant, frictionless fill at the signal bar's own close -- a new
`ExecutionModel` (`src/backtesting/execution_model.py`) makes fill
timing, slippage, and fees explicit and deterministic. Two typed timing
conventions: `SIGNAL_BAR_CLOSE` (the default, reproducing exactly what
`run_portfolio()` always did, so no existing result or test is silently
reinterpreted) and `NEXT_BAR_OPEN` (the new realistic mode -- the
reference price is strictly the *next* candle's own `open`, never any
value from a bar the signal couldn't have seen). No next bar, or a
signal timestamp outside the candle index, is an explicit
`NO_EXECUTION_BAR` outcome, never a silent same-bar-close fallback.
`PercentageSlippageModel`/`PercentageFeeModel` are deterministic
(zero-cost is a first-class, non-hardcoded-minimum configuration; buy
and sell slippage move price in genuinely opposite directions, never
the same formula with a sign flip) and reuse the existing
`Fill`/`Order`/`OrderSide` shapes from `src/execution/models.py` --
`Fill` gained four new, defaulted fields
(`reference_price`/`fill_timestamp`/`slippage_amount`/`fee`), and every
pre-Sprint-12 `PaperBroker` call site is unaffected. `Trade.entry_price`/
`exit_price`/`gross_pnl` remain exactly the frictionless reference-price
economics ADR-0044 established; new `entry_fill_price`/
`exit_fill_price`/`entry_fee`/`exit_fee` fields plus `total_fees`/
`slippage_cost`/`net_pnl` properties surface the real, cost-aware
economics without ever mutating the reference fields. `Portfolio.
open_position()`/`close_position()` gained an optional `fee` parameter,
debited as a genuine cash movement. A new
`SignalOutcome.execution_unavailable_reason` (mutually exclusive with
the existing `stop_unavailable_reason`) reports `NO_EXECUTION_BAR`/
`INSUFFICIENT_CASH_FOR_FEE` when Risk approved a trade but Execution
couldn't actually fill it -- `PortfolioRiskEngine`'s own sizing
arithmetic is completely untouched. `BacktestConfig.execution_config`
(defaulting to a fresh zero-cost `ExecutionConfig`) and
`src/analytics`'s new `has_execution_cost_detail`/`total_fees_dollars`/
`total_slippage_cost_dollars`/`net_pnl_after_costs_dollars` round out
provenance and reporting; `ExperimentSpec.backtest_config` needed zero
schema change. `AISignalStrategy` required zero special-casing --
re-verified with a dedicated cost-aware end-to-end test. Order-book
simulation, partial fills, limit/stop orders, and broker-specific fee
schedules remain explicitly out of scope. See `DECISIONS.md`, ADR-0045
for the full design and reasoning._

_As of 2026-09-22 -- **Sprint 11 (Portfolio-Aware Backtesting &
Unified Risk Simulation) is implemented and confirmed via real `pytest`
on the dev machine: 850 passed, 0 failed (Python 3.14.6, pytest 9.1.1,
4.57s, no skips -- every scikit-learn/joblib/Streamlit-gated test that
the sandbox could only skip ran for real here, and both of the
sandbox's "known environment-only" failures did not reproduce, exactly
as every prior sprint predicted).** The Backtester now has a
second, genuinely risk-sized execution mode alongside the original
Sprint 2 unit-sized one: `Backtester.run_portfolio()` (new
`PortfolioBacktestEngine`, `src/backtesting/portfolio_engine.py`) runs
one or more strategies against a single shared, real `Portfolio`
through the exact same `PortfolioRiskEngine` (Sprint 7) that already
governs paper trading -- entries via `.decide()`, exits via
`.decide_close()`, quantity always Risk's decision, never the
Backtester's own. A new `BacktestConfig`/`RiskMode` (`LEGACY_UNIT` vs.
`PORTFOLIO_RISK`) makes which sizing model produced a given
`BacktestResult` explicit and typed, never inferred; the original
`Backtester.run()` is byte-for-byte unchanged and still produces
`LEGACY_UNIT` results by default. A new `StopPolicy` abstraction
(`src/backtesting/stop_policy.py`, `ATRStopPolicy` the one concrete
implementation) supplies the stop boundary `PortfolioRiskEngine.decide()`
requires without widening `Signal`'s contract -- past-only by
construction (the caller always truncates history to the signal's own
timestamp before handing it over), reporting `STOP_UNAVAILABLE`
explicitly rather than ever falling back to unit sizing. `Trade` gained
an optional `quantity` field (and a `gross_pnl` property) -- `None` for
every `LEGACY_UNIT` trade, the real approved/filled share count for a
`PORTFOLIO_RISK` one -- and `BacktestResult` gained `risk_mode`,
`backtest_config`, `signal_outcomes` (per-signal accept/reject
auditability, `src/backtesting/risk_audit.py`), and `final_portfolio`.
`ExperimentRegistry`'s `experiment_trades` table gained a nullable
`quantity` column (Portfolio/position persistence needed no changes --
it already round-tripped `quantity`/`side`/`stop_price` generically
since Sprint 9); `src/analytics` gained dollar-denominated
`net_pnl_dollars`/`gross_profit_dollars`/`gross_loss_dollars`/
`expectancy_dollars`/`average_winner_dollars`/`average_loser_dollars`/
`largest_winner_dollars`/`largest_loser_dollars`, gated on a new
`has_quantity_detail()` check so a `LEGACY_UNIT` trade list is never
silently treated as sized -- the original fraction-based metrics are
completely unchanged and still populate for every trade list regardless
of risk mode. `AISignalStrategy` required zero special-casing anywhere
in the new engine -- it, `EMACrossStrategy`, and
`RSIMeanReversionStrategy` all traverse the identical
`Strategy -> Signal -> PortfolioRiskEngine -> Portfolio` path, confirmed
by both a dedicated AI end-to-end test through `run_portfolio()` and a
new architecture test scanning `portfolio_engine.py`'s own source for
any AI-specific mention. Execution realism (slippage, commissions,
partial fills, order-book simulation) and advanced portfolio management
(Kelly sizing, VaR/CVaR, correlation optimization) remain explicitly
out of scope -- this sprint closes the risk-sizing gap between research
and paper trading, not the execution-realism gap. See `DECISIONS.md`,
ADR-0044 for the full design and reasoning._

_As of 2026-09-22 -- **Sprint 10 (ML Signal Research & AI
Strategy Integration) is complete and confirmed via real `pytest` on
the dev machine: 784 passed, 0 failed, all green.** A new `src/ai/` package
(`features.py`/`labels.py`/`dataset.py`/`splitting.py`/`model.py`/
`identity.py`/`artifacts.py`/`registry.py`/`evaluation.py`/
`training.py`) is a reproducible, leakage-safe ML research layer:
past-only engineered features, a separately-configured 3-class
forward-return label, chronological train/validation/test splitting
with a purge/embargo (never shuffled), a `scikit-learn`
`LogisticRegression` baseline behind an extensible `AIModel` interface,
deterministic model identity, joblib artifact persistence, and a
JSON-metadata `ModelRegistry`. A new `src/strategies/ai_signal.py`
(`AISignalStrategy`) is the entire seam to the rest of the platform --
loads an already-trained, frozen model and maps its predictions onto
the existing `Signal`/`SignalDirection`, sparsely, with model
provenance in signal metadata; it never trains, never sizes a
position, never touches an order. `Backtester`/`src.risk`/
`src.execution`/`src.portfolio`/`src.dashboard` gained zero AI-specific
code -- AI is one more signal source, not a replacement for any of
them (an architecture test enforces this structurally). No LLM
anywhere in this chain; the existing AI Research Reporter's separate
LLM/fallback narrative path is untouched. `scikit-learn`/`joblib` are
real `requirements.txt` dependencies but aren't installed in this
sandbox (same gap Streamlit had for Sprint 9) -- every test needing a
real model fit is gated with `pytest.importorskip`, skips cleanly here,
and is pending confirmation on the dev machine. Sandbox suite: **725
passed**, 2 known environment-only failures (unchanged from prior
sprints), 8 skipped. Full design in `DECISIONS.md`, ADR-0043._

_As of 2026-09-16 -- **Sprint 8 (Market Data Integrity &
Session Awareness), including its canonicalization-idempotence
cleanup, is complete and confirmed via real `pytest` on the dev
machine: 572 passed, 0 failed, 0 errors.** Resolves two long-deferred ADRs (ADR-0006
data validation, ADR-0007 incremental cache) and the timezone-policy
question ADR-0019/ADR-0038 both referenced but left open. Internal
candle timestamps are now timezone-aware UTC everywhere, always -- a
full migration through `DataProvider`/`MarketDataService`'s contract,
not a scoped-down partial fix (`DECISIONS.md`, ADR-0041). A new
`src/data/validation.py` rejects structurally invalid or financially
impossible candle data outright (`StructuralValidationError`/
`FinancialSanityError`) and flags gaps/zero-volume as non-fatal
`ValidationReport` entries, session-aware via a new top-level
`src/calendar/` package (`TradingCalendar`/`NYSECalendar` -- regular
US equity session, 09:30-16:00 America/New_York, holidays from a
hand-rolled rule table rather than a calendar-library dependency).
`src/data/canonical.py`'s `canonicalize_candles()` is now the single
choke point both hashing (`dataframe_fingerprint()`) and caching run
through, so a cache hit and a fresh fetch of identical data always
fingerprint identically. The on-disk cache moved from exact-range keys
to `(symbol, interval)`-keyed incremental fetch (fetch only the new
tail when a request's range extends, per ADR-0007). `get_candles()`/
`get_history()` keep their exact pre-Sprint-8 signatures; the new
`get_dataset()` returns the full `CandleDataset` (candles plus
identity/session/quality metadata) for callers that want it, and
`Backtester.run()` gained an optional `dataset` parameter so a
`BacktestResult` can carry a `dataset_identity` -- both purely
additive, no existing call site required a change.

**Sprint 7 (Portfolio-Aware Risk & Position Management) is complete and
confirmed.** The platform moved from simple per-trade
allocation toward genuine, stop-based position sizing plus
portfolio-level exposure constraints: `PortfolioRiskEngine`
(`src/risk/portfolio_risk.py`) sizes a position from a defined stop-loss
distance (`risk_quantity = floor(equity * risk_pct_per_trade /
abs(entry_price - stop_price))`, a hard ceiling) and then reduces that
quantity -- never increases it -- against independently-computed
capital, allocation, total-exposure, and symbol-exposure ceilings,
returning a structured, fully-auditable `RiskDecision`
(`src/risk/models.py`) rather than a boolean and a string. A new,
neutral `Portfolio` domain model (`src/portfolio/models.py`) and
platform-level `Position` (`src/portfolio/position.py`) track cash,
open positions, and exposure, deliberately coexisting with (not
replacing) `PaperBroker`'s own bookkeeping -- `src/execution/
portfolio_sync.py`'s `apply_fill_to_portfolio()` is the small glue
keeping the two in sync. `PositionSizer`, `RiskLimits`,
`SizingDecision`, `AccountState`, `PaperBroker`, and every existing
broker are byte-for-byte unchanged -- every Sprint 7 addition is new,
standalone code (`DECISIONS.md`, ADR-0039). This superseded the
`ROADMAP.md`-planned "Analytics & Dashboard" Sprint 7 scope, which is
re-sequenced (not abandoned) below.

A follow-up cleanup pass (`DECISIONS.md`, ADR-0040) made two review
items explicit rather than implicit: (1) `PortfolioRiskEngine.
decide_close()` is now the formal, symmetric counterpart to `decide()`
for exit/close intent -- a lookup-and-permit operation, never
risk-sized, never gated by exposure limits, returning
`RejectionReason.NO_POSITION_TO_CLOSE` when there's nothing to close
rather than raising or fabricating a close order; (2) a `SHORT`'s
unmodeled capital/margin ceiling is now typed
(`CapitalConstraintModel.NOT_MODELED`) rather than an ambiguous `None`
a reader could misinterpret as "unlimited." The cleanup also formalized
the Risk -> Execution handoff (`RiskDecision.to_trade_intent() ->
ApprovedTradeIntent`, enforcing in code that execution can never be
handed more than Risk approved) and added a static test confirming
`src/execution` never imports the risk-computation symbols. Nothing
existing was redesigned; `decide()` still raises on `FLAT`, unchanged.
Confirmed via the sandbox stub-based test runner: **497 passed**, 2
known environment-only failures
(`test_python_version_passes_against_running_interpreter`,
`test_logger_is_importable_and_callable`) -- up from ADR-0039's 470 and
the Pre-Sprint 7 baseline of 402. **Confirmed via real `pytest` on the
dev machine (Python 3.14.6, pytest 9.1.1): 499 passed in 1.18s, all
green** -- including both tests that failed only in the sandbox,
confirming they were genuinely environment-only artifacts (sandbox
Python 3.10 vs. the project's pin; the sandbox's own no-op `loguru`
stub), not real regressions. Sprint 7, including this cleanup, is
confirmed and ready to commit._

_Pre-Sprint 7: the research/strategy
architecture is now confirmed timeframe-agnostic** (`DECISIONS.md`,
ADR-0038). The platform architecturally supports daily/swing, intraday,
and minute-scale trading using the same conceptual Strategy/Backtester/
ExperimentSpec interfaces -- SPY/1d and SPY/1m run through the identical
`EMACrossStrategy` code unmodified. Two genuine gaps were found and
fixed: `calculate_metrics()`/`sharpe_ratio()` (`src/backtesting/metrics.py`)
annualized every backtest with a hardcoded `252` (trading days/year)
regardless of actual bar size -- silently wrong for intraday data --
now inferred from the candles' own timestamp spacing
(`infer_periods_per_year()`), unchanged for daily bars; and
`ExperimentSpec.interval` was an untyped, independently-hardcoded
string -- now a typed `Interval` (`src/data/base.py`), with
`scripts/run_experiment.py` gaining a `--interval` flag so the fetched
data and the recorded experiment timeframe can never silently disagree.
Everything else inspected -- `Signal`/`Trade`/`ExperimentSpec` timestamp
precision, the `Strategy` contract, `Backtester`'s row-based execution
model, `CacheManager`'s CSV round-trip, `YFinanceProvider`'s
normalization -- was already timeframe-agnostic and required no change.
**This is architectural readiness, not new functionality**: no live
intraday trading, no tick feeds, no order-book simulation, and no
second-scale/HFT execution were built or are implemented -- those
remain explicitly future work. A formal Acceptance Criteria document
(AC-01 through AC-19) was subsequently supplied and closed with 9
dedicated tests (`tests/test_intraday_acceptance.py`) providing
literal, per-criterion evidence -- see the sprint completion report.
Confirmed via real `pytest` on the dev machine (Python 3.14.6):
**402 passed in 1.12s**, all green._

_**Sprint 6 (Research Pipeline & Experiment
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
- ✅ Pre-Sprint 7 -- Timeframe-Agnostic Architecture Corrections
  (`DECISIONS.md`, ADR-0038). A review confirmed the research/strategy
  architecture already supported daily and intraday timeframes almost
  everywhere -- `Signal`/`Trade`/`ExperimentSpec` timestamps are all
  full-precision `pd.Timestamp`, the `Strategy` contract never assumes
  one signal per day, and `Backtester` already treats candles as an
  ordered row sequence rather than "one row = one trading day" -- and
  fixed the two genuine gaps found: `calculate_metrics()`/
  `sharpe_ratio()` (`src/backtesting/metrics.py`) annualized Sharpe with
  a hardcoded `252` regardless of actual bar size (now inferred from
  the data's own timestamp spacing via `infer_periods_per_year()`,
  unchanged for daily bars, corrected for intraday); and
  `ExperimentSpec.interval` was an untyped string independently
  hardcoded in `scripts/run_experiment.py` (now a typed `Interval`,
  `src/data/base.py`, with a `--interval` CLI flag threading the same
  value to both the fetch and the recorded spec). New
  `tests/test_timeframe_agnostic.py` (6 tests) proves the identical
  `EMACrossStrategy` code runs against daily and 1-minute fixtures
  unmodified, two signals minutes apart within one session both survive
  as a single precisely-timed trade, a 5.5-minute intraday hold
  attributes correctly, and Sharpe annualization now scales with bar
  frequency. **Architectural readiness, not new functionality**: no
  live intraday trading, tick feeds, order-book simulation, or
  second-scale/HFT execution were built -- those remain explicit future
  work. Extension Cost: 4 existing files touched
  (`src/backtesting/metrics.py`, `src/backtesting/engine.py`,
  `src/experiments/spec.py`, `src/experiments/registry.py`) plus
  `scripts/run_experiment.py`, plus `tests/test_intraday_acceptance.py`
  (9 tests) providing literal, per-criterion evidence against the
  formal Acceptance Criteria (AC-01 through AC-19) document. Confirmed
  via real `pytest` on the dev machine: **402 passed in 1.12s**, all
  green.
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

- ✅ Sprint 7 -- Portfolio-Aware Risk & Position Management
  (`DECISIONS.md`, ADR-0039). `PortfolioRiskEngine.decide()`
  (`src/risk/portfolio_risk.py`) sizes a `LONG`/`SHORT` signal from a
  stop-loss distance (Stage A: `risk_quantity =
  floor(equity * risk_pct_per_trade / abs(entry_price - stop_price))`,
  a ceiling nothing may exceed) then applies independently-computed
  capital/allocation/total-exposure/symbol-exposure ceilings (Stage B:
  `min()`), returning a structured `RiskDecision`
  (`src/risk/models.py`, 13-member `RejectionReason` enum) rather than
  a boolean. Standalone from `PositionSizer` -- `RiskLimits`,
  `SizingDecision`, and the existing allocation-only model are
  untouched. New `Portfolio`/`Position` domain models
  (`src/portfolio/models.py`, `src/portfolio/position.py`) track cash,
  positions, and exposure, deliberately duplicating (not replacing)
  `PaperBroker`'s own bookkeeping;
  `src/execution/portfolio_sync.py::apply_fill_to_portfolio()` is the
  glue keeping the two in sync, living on the allowed side of
  `src/portfolio`'s dependency-isolation boundary. Position scaling
  (adding to or partially reducing an existing position) stays
  unsupported, matching `PaperBroker`; closing bypasses the risk engine
  entirely, exactly as before. New tests:
  `tests/test_portfolio_position.py` (27),
  `tests/test_portfolio_risk.py` (36, including all six of the spec's
  mandatory worked examples and the "approved quantity never exceeds
  risk quantity" architectural invariant across five scenarios),
  `tests/test_sprint7_integration.py` (6), plus a new
  `tests/test_architecture.py` check confirming `src/risk` never
  imports `src/broker`/`src/execution`. Extension Cost (ADR-0014):
  three new files (`src/portfolio/position.py`,
  `src/risk/portfolio_risk.py`, `src/execution/portfolio_sync.py`), new
  classes appended to two existing files
  (`src/portfolio/models.py`, `src/risk/models.py`), `__init__.py`
  export updates in `src/portfolio`/`src/risk`/`src/execution` -- no
  existing class modified. Confirmed via the sandbox stub-based test
  runner: **470 passed**, 2 known environment-only failures. Pending
  real-`pytest` confirmation on the dev machine.
- ✅ Sprint 8 -- Market Data Integrity & Session Awareness
  (`DECISIONS.md`, ADR-0041). Resolves ADR-0006 (validation) and
  ADR-0007 (incremental cache), plus the timezone-policy question
  ADR-0019/ADR-0038 left open. `src/calendar/` (new package) --
  `TradingCalendar`/`NYSECalendar`, regular US equity session,
  hand-rolled holiday rule table. `src/data/canonical.py`'s
  `canonicalize_candles()` -- the single choke point for hashing and
  caching, so a cache hit and a fresh fetch fingerprint identically.
  `src/data/validation.py`'s `validate_candles()` -- structural/
  financial-sanity checks raise (`StructuralValidationError`/
  `FinancialSanityError`), gaps/zero-volume are non-fatal
  `ValidationReport` entries. Internal timestamps are timezone-aware
  UTC everywhere now -- a full migration through
  `DataProvider`/`MarketDataService`'s contract. Cache re-keyed to
  `(symbol, interval)` with incremental tail-only fetch.
  `get_candles()`/`get_history()` signatures unchanged;
  `get_dataset()` (new) returns the full `CandleDataset`;
  `Backtester.run()` gained an optional `dataset` parameter for
  `BacktestResult.dataset_identity` -- both additive, no existing call
  site changed. 70 new tests (`tests/test_calendar.py`: 19,
  `tests/test_data_validation.py`: 24, `tests/test_data_canonical.py`:
  13, plus 11 added to `tests/test_market_data.py` and 3 added to
  `tests/test_architecture.py`). Confirmed via the sandbox stub-based
  test runner: **567 passed**, 2 known environment-only failures. The
  real `pytest` run on the dev machine (Python 3.14.6, pytest 9.1.1)
  actually returned **1 failed, 568 passed** -- an earlier version of
  this entry wrongly claimed "569 passed, 0 failed" before that run had
  been reported; see the Sprint 8 cleanup note below for the correction,
  root cause, and fix. Real-`pytest` reconfirmation after the fix is
  pending.
- ✅ Sprint 8 cleanup -- fixed the one real failure from that dev-machine
  run (`DECISIONS.md`, ADR-0041 correction). Root cause:
  `test_data_canonical.test_canonicalize_is_idempotent` compared two
  canonicalization passes with `assert_frame_equal()` (default
  `check_freq=True`); their `DatetimeIndex.freq` attributes differed
  (`<Day>` vs. `None`) even though values/columns/dtypes/content-hash
  were all identical, because `tz_localize()` (first pass) and
  `tz_convert()` (second pass) aren't guaranteed to preserve `.freq`
  identically across pandas versions. Fixed in production code, not by
  relaxing the test: `canonicalize_candles()`
  (`src/data/canonical.py`) now explicitly pins `DatetimeIndex.freq` to
  `None` on its output, so repeated canonicalization can never
  reintroduce a freq disagreement. Three regression tests added:
  `test_canonicalize_clears_inferred_datetimeindex_freq`,
  `test_hash_of_canonical_output_is_stable_under_a_second_canonicalization_pass`,
  and `test_incremental_fetch_produces_the_same_hash_as_a_complete_fetch`
  (this last one closes a real gap -- the existing incremental-fetch
  tests checked call counts and date ranges, not that the assembled
  result hashes identically to a one-shot fetch of the same range).
  Sandbox-reverified: **570 passed** (567 + 3 new tests), same 2 known
  environment-only failures. Confirmed via real `pytest` on the dev
  machine: **572 passed, 0 failed, 0 errors, all green** -- both
  previously sandbox-only artifacts passed for real. This cleanup is
  confirmed.
- ✅ Sprint 7 cleanup -- explicit close-path semantics, explicit
  short-margin representation, formalized Risk/Execution boundary
  (`DECISIONS.md`, ADR-0040). `PortfolioRiskEngine.decide_close()` is
  the new, symmetric close/exit-intent counterpart to `decide()` --
  never risk-sized, never gated by `max_portfolio_exposure_pct`/
  `max_symbol_exposure_pct` (a close is permitted even over a breached
  limit), returning `RejectionReason.NO_POSITION_TO_CLOSE` when nothing
  is held and `UNSUPPORTED_POSITION_OPERATION` for any non-full
  quantity (no partial-close support fabricated). `CapitalConstraintModel`
  (`MODELED`/`NOT_MODELED`) makes a `SHORT`'s unmodeled capital ceiling
  typed rather than an ambiguous `None`. `RiskDecision.to_trade_intent()
  -> ApprovedTradeIntent` formalizes the Risk -> Execution handoff and
  enforces `execution_quantity <= risk_approved_quantity` in code. No
  existing behavior changed -- `decide()` still raises on `FLAT`,
  `PaperBroker`/`Portfolio`/`RiskLimits`/`SizingDecision` untouched. 27
  new tests (22 in `tests/test_portfolio_risk.py`, 4 in
  `tests/test_sprint7_integration.py`, 1 in `tests/test_architecture.py`).
  Confirmed via the sandbox stub-based test runner: **497 passed**, 2
  known environment-only failures. Confirmed via real `pytest` on the
  dev machine (Python 3.14.6, pytest 9.1.1): **499 passed in 1.18s, all
  green.**
- ✅ Sprint 9 -- Analytics & Dashboard (`DECISIONS.md`, ADR-0042).
  Resolves `ROADMAP.md`'s "Analytics & Dashboard vs. AI" ambiguity in
  favor of Analytics & Dashboard; AI/ML signal generation remains
  explicit future work. `ExperimentRegistry` gained four additive
  tables (`experiment_trades`/`experiment_equity_curves`/
  `experiment_portfolios`/`experiment_research_reports`) so a past
  experiment's trades, equity curve, resulting `Portfolio`, and
  research report survive past the process that ran it -- an explicit
  "nothing here" value (`[]`/empty `pd.Series`/`None`) for anything
  logged before Sprint 9. `Portfolio.reconstruct()` (new classmethod)
  restores an already-known snapshot without going through the
  fill-simulating `open_position()`/`close_position()`. New
  `src/analytics/` package: `Metric`/`MetricStatus` make every computed
  number explicitly `OK` or `UNDEFINED` with a reason (never a silent
  `0`/`inf`/`nan`); `metrics.py`'s pure functions compute total P&L,
  total return, win rate, profit factor, expectancy, average/largest
  winner/loser, max drawdown (and the full `drawdown_curve()` it's
  built on), Sharpe, volatility, and exposure time -- Sharpe/volatility
  reuse `infer_periods_per_year()` (ADR-0038) for timeframe-aware
  annualization; `AnalyticsService.analyze_backtest()`/
  `analyze_experiment()` and `compare_experiments()` are the one thing
  a caller talks to, with comparison warnings (never a composite "best
  strategy" score) for material symbol/timeframe/dataset/strategy
  differences; `PortfolioValuationService.value()` is strictly
  read-only mark-to-market valuation, with any open position lacking a
  current price making the *aggregate* metrics `UNDEFINED` rather than
  a silently partial sum. New `src/dashboard/` package -- a read-only
  Streamlit app (`streamlit run src/dashboard/app.py`) with four pages
  (Overview, Backtest/Experiment Analysis, Strategy Comparison, Paper
  Portfolio) that never computes a metric itself and cannot submit an
  order, change a risk limit, or mutate any state; the only sanctioned
  `MarketDataService` touchpoint anywhere in either package is
  `src.analytics.valuation.latest_price()`/`default_price_lookup()`.
  New tests: `tests/test_analytics.py` (59), `tests/test_portfolio_valuation.py`
  (13), `tests/test_dashboard_formatting.py` (16, unconditional),
  `tests/test_dashboard_smoke.py` (9, `pytest.importorskip("streamlit")`-
  gated, using `streamlit.testing.v1.AppTest`, fully network-free), 12
  added to `tests/test_experiments.py`, 2 added to
  `tests/test_run_experiment_script.py`, 8 added to
  `tests/test_architecture.py` for the new package-boundary
  invariants. Extension Cost (ADR-0014): two new top-level packages,
  four new tables + eight new methods on `ExperimentRegistry`, one new
  classmethod on `Portfolio`, `scripts/run_experiment.py` extended at
  its existing single write point -- no existing method signature
  changed except that script's own internal helper. Confirmed via the
  sandbox stub-based test runner: **677 passed**, 2 known
  environment-only failures, 1 correctly-skipped module (no Streamlit
  in this sandbox). The first real `pytest` run found 6 failures, all
  in `tests/test_dashboard_smoke.py` -- a test-isolation cache bug, now
  fixed; real-`pytest` reconfirmation is pending (see `DECISIONS.md`,
  ADR-0042).
- ✅ Sprint 10 -- ML Signal Research & AI Strategy Integration
  (`DECISIONS.md`, ADR-0043). `src/ai/` (nine modules) is a
  research-only ML capability -- past-only engineered features
  (`features.py`), a separately-configured 3-class forward-return label
  (`labels.py`), a leakage-free training table (`dataset.py`),
  chronological split with purge/embargo (`splitting.py`), an
  extensible `AIModel` interface wrapping a `scikit-learn` `Pipeline`
  (`model.py`), deterministic model identity/artifact persistence/a
  JSON-metadata registry (`identity.py`/`artifacts.py`/`registry.py`),
  and `MLTrainingService` as the one application-facing entry point
  (`training.py`), classification-quality metrics kept strictly
  separate from trading performance (`evaluation.py`).
  `src/strategies/ai_signal.py`'s `AISignalStrategy` is the entire seam
  to the rest of the platform -- loads an already-trained, frozen model
  and maps its predictions onto the existing `Signal`/`SignalDirection`,
  sparsely, with model provenance in signal metadata; it never trains,
  never sizes a position, never touches an order.
  `Backtester`/`src.risk`/`src.execution`/`src.portfolio`/`src.dashboard`
  gained zero AI-specific code, confirmed structurally by an
  architecture test. Confirmed via real `pytest` on the dev machine:
  **784 passed, 0 failed, all green.**

## Current Module

**Sprint 13 (AI Research Agent) is implemented and sandbox-confirmed:
897 passed, 2 known environment-only failures, 13 skipped** (network-
free; `FakeLLMProvider` only; 7 of the 13 skips are this sprint's own
`joblib`-gated test files, following the exact convention every
sklearn/joblib/streamlit-gated module in this suite already uses).
`src.ai.agents.agent.ResearchAgent` implements an explicit, bounded
state-machine loop -- `INITIAL -> SEND_TO_MODEL -> (MODEL_REQUESTS_TOOL
-> VALIDATE_TOOL -> CHECK_POLICY -> EXECUTE_TOOL -> APPEND_RESULT ->
SEND_TO_MODEL)* -> FINAL_RESPONSE` -- driven entirely by a
provider-neutral `LLMProvider` protocol (`src.ai.agents.provider`), with
`AnthropicLLMProvider` (`src.ai.agents.anthropic_provider`) as the first
concrete, fully optional adapter (`anthropic` imported lazily, never
added to `requirements.txt`, mirroring `ClaudeNarrativeRenderer`'s own
convention from ADR-0020). `ResearchAgentPolicy` enforces `max_steps=12`/
`max_backtests=4` budgets and structurally forbids live trading,
portfolio/risk mutation, model training, and strategy generation in
code, not merely in the system prompt -- and there is, in any case, no
tool anywhere in this package implementing any of those capabilities,
so the restriction holds even if a policy flag were somehow flipped.
Seven tools exist: six read-only (`list_experiments`/`get_experiment`/
`analyze_experiment`/`compare_experiments`/`list_strategies`/
`get_model_metadata`, each delegating to an existing application
service, never recomputing a metric) plus `run_historical_backtest` --
the one tool that produces new evidence, via a new, generic
`src.research.trial_service.ResearchTrialService` (`MarketDataService ->
Strategy -> PortfolioBacktestEngine -> Risk -> Execution -> Analytics`,
never a second backtest pipeline, never `scripts/run_experiment.py`).
Every research trial is ephemeral; nothing is auto-persisted to
`ExperimentRegistry`. Every `AgentRun` (`src.ai.agents.models`) carries
full configuration provenance (`system_prompt_version`/
`tool_schema_version`/`policy_hash`, alongside `provider`/`model`) and
is persisted (one JSON file per run, no hidden chain-of-thought) via
`AgentRunStore` under `data/agent_runs/` (already covered by
`.gitignore`'s wholesale `data/*` rule). A new CLI entry point,
`python -m src.cli research-agent --goal "..."`, fails clearly (exit
code 2) without `ANTHROPIC_API_KEY` configured. See `DECISIONS.md`,
ADR-0046 for the complete design and reasoning.

_Previously: Sprint 12 (Execution Realism & Transaction Cost Modeling) is
implemented and confirmed via real `pytest` on the dev machine: 916
passed, 0 failed.** (Sandbox had reported 855 passed, 2 known
environment-only failures, 8 skipped -- the dev machine ran every
scikit-learn/joblib/Streamlit-gated test the sandbox could only skip,
and neither known environment-only failure reproduced.) The
`run_portfolio()` path no longer assumes an instant,
frictionless fill at the signal bar's own close: a new `ExecutionModel`
(`src/backtesting/execution_model.py`) makes fill timing (`SIGNAL_BAR_
CLOSE` default vs. `NEXT_BAR_OPEN`), slippage
(`PercentageSlippageModel`), and fees (`PercentageFeeModel`) explicit,
deterministic, and configurable via a new `ExecutionConfig` carried on
`BacktestConfig.execution_config` (default: zero-cost,
`SIGNAL_BAR_CLOSE` -- reproducing exactly what `run_portfolio()` always
did before this sprint, so no existing result is silently
reinterpreted). `NEXT_BAR_OPEN`'s reference price is structurally
guaranteed to be only the next candle's own `open` -- the implementation
has no code path that reads a future bar's high/low/close, proven
directly by a look-ahead-protection regression test. No next bar (the
final candle in a series) or a signal timestamp outside the candle
index is an explicit `NO_EXECUTION_BAR` outcome, never a silent
same-bar-close fallback; a fee that would push cash negative is an
explicit `INSUFFICIENT_CASH_FOR_FEE` outcome, never a silently negative
`Portfolio.cash`. Both surface through a new
`SignalOutcome.execution_unavailable_reason`, mutually exclusive with
the existing `stop_unavailable_reason` -- `PortfolioRiskEngine`'s own
sizing arithmetic is completely untouched by any of this.
`src/execution/models.py`'s existing `Fill` gained four new, defaulted
fields (`reference_price`/`fill_timestamp`/`slippage_amount`/`fee`) --
reused, never duplicated into a second fill model -- so every
pre-Sprint-12 `PaperBroker` call site is unaffected.
`Trade.entry_price`/`exit_price`/`gross_pnl` remain exactly the
frictionless reference-price economics ADR-0044 established; new
`entry_fill_price`/`exit_fill_price`/`entry_fee`/`exit_fee` fields plus
`total_fees`/`slippage_cost`/`net_pnl` properties surface the real,
cost-aware economics without ever mutating the reference fields --
under the zero-cost default, `entry_fill_price == entry_price` and
`net_pnl == gross_pnl` exactly, so no pre-Sprint-12 trade's numbers
change. `Portfolio.open_position()`/`close_position()` gained an
optional `fee` parameter, debited as a genuine cash movement alongside
the existing quantity-times-price one. `src/analytics` gained
`has_execution_cost_detail`/`total_fees_dollars`/
`total_slippage_cost_dollars`/`net_pnl_after_costs_dollars`, gated
appropriately so a run without real fill-price detail reports
`Metric.undefined(...)` rather than a fabricated cost figure.
`ExperimentSpec.backtest_config` needed zero schema change --
`BacktestConfig.describe()`'s new `"execution"` key round-trips through
the existing reserved JSON field. `AISignalStrategy` required zero
special-casing, re-verified with a dedicated cost-aware end-to-end
test through the exact same path `EMACrossStrategy`/
`RSIMeanReversionStrategy` use. New tests: `tests/test_execution_model.py`
(27, unit-level timing/look-ahead/slippage/fee/purity coverage), 15
added to `tests/test_portfolio_backtest_engine.py` (cost-aware
integration: reference-vs-fill-price distinction, quantity invariant,
portfolio cash, gross-vs-net P&L, multi-trade fee totals,
reproducibility, session-boundary Friday-to-Monday, `NO_EXECUTION_BAR`,
`INSUFFICIENT_CASH_FOR_FEE`), 9 added to `tests/test_portfolio_
position.py` (the new `fee` parameter), 12 added to `tests/test_
analytics.py` (the 4 new execution-cost metrics), 6 added to
`tests/test_architecture.py` (execution-boundary protection: broker-
independence, no risk/signal imports, purity, Fill/Order reuse), and 1
added to `tests/test_ai_end_to_end.py` (cost-aware AI compatibility).
Partial fills, limit/stop orders, order-book simulation, and
broker-specific fee schedules remain explicitly out of scope -- this
sprint models generic candle-level research friction, not an exchange.
See `DECISIONS.md`, ADR-0045 for the full design and reasoning.

**Sprint 11 (Portfolio-Aware Backtesting & Unified Risk Simulation) is
implemented and confirmed via real `pytest` on the dev machine: 850
passed, 0 failed.** (Sandbox had reported 790 passed, 2 known
environment-only failures, 8 skipped -- the dev machine ran every
scikit-learn/joblib/Streamlit-gated test the sandbox could only skip,
and neither known environment-only failure reproduced.) The same risk model
that constrains paper trading now constrains historical research:
`Backtester.run_portfolio()` (`PortfolioBacktestEngine`,
`src/backtesting/portfolio_engine.py`) processes one or more strategies'
sparse signals chronologically against a single shared, fresh,
isolated `Portfolio`, sizing every entry through the real
`PortfolioRiskEngine.decide()` (never a reimplemented formula) and
every exit through `.decide_close()` -- the identical engine Sprint 7
built for paper trading, replayed against historical candles instead of
live ones. `Backtester.run()`, the original Sprint 2 one-unit-per-signal
model, is completely unchanged and remains the default; a new
`BacktestConfig`/`RiskMode` pair (`LEGACY_UNIT` vs. `PORTFOLIO_RISK`)
makes which model produced a given `BacktestResult` explicit, typed,
and impossible to mistake for the other. A new `StopPolicy` abstraction
(`src/backtesting/stop_policy.py`) supplies the sizing-boundary stop
`PortfolioRiskEngine.decide()` requires -- `ATRStopPolicy` computes it
via the existing `IndicatorEngine`, structurally leakage-safe (the
caller always truncates history to the signal's own timestamp before
handing it over, so no implementation can see the future by accident),
reporting `STOP_UNAVAILABLE` rather than ever inventing a fallback unit
size when ATR hasn't warmed up. `Trade` gained an optional `quantity`
field (`None` for every `LEGACY_UNIT` trade, the real approved/filled
share count for `PORTFOLIO_RISK`) and a `gross_pnl` property;
`BacktestResult` gained `risk_mode`/`backtest_config`/`signal_outcomes`
(full per-signal accept/reject auditability, distinguishing "signal
generated" from "trade approved" from "trade rejected," never collapsed
into a bare exception string, `src/backtesting/risk_audit.py`) and
`final_portfolio`. `ExperimentRegistry`'s `experiment_trades` table
gained one nullable `quantity` column -- Portfolio/position persistence
needed no schema change at all, since it already round-tripped
`quantity`/`side`/`stop_price` generically since Sprint 9, and
`ExperimentSpec.backtest_config` was already a reserved, extensible
JSON field since Sprint 6. `src/analytics` gained eight
dollar-denominated metrics (net/gross P&L, expectancy, average/largest
winner/loser), gated on a new `has_quantity_detail()` check so a
`LEGACY_UNIT` trade list is never silently treated as sized -- the
original fraction-based metrics are completely unchanged for every risk
mode. `AISignalStrategy` required zero special-casing anywhere in the
new engine, confirmed by a dedicated end-to-end test through
`run_portfolio()` plus a new architecture test scanning
`portfolio_engine.py`'s own source for any AI-specific mention -- it,
`EMACrossStrategy`, and `RSIMeanReversionStrategy` all traverse the
identical `Strategy -> Signal -> PortfolioRiskEngine -> Portfolio` path,
since all three emit the same canonical `Signal`. Reversal/position-
scaling stays explicitly out of scope this sprint -- a same-symbol
signal while a position is already open is rejected by the existing,
unmodified `PortfolioRiskEngine.decide()`'s own
`POSITION_SCALING_NOT_SUPPORTED` check, with no new reversal-handling
logic added anywhere. New tests: `tests/test_stop_policy.py` (16,
including a leakage-regression test proving a stop computed at time
`t` is unaffected by candle data after `t`), `tests/test_portfolio_
backtest_engine.py` (28, covering concurrent-position-limit enforcement,
exposure-limit enforcement via the real engine, a hard-ceiling
regression across multiple constrained scenarios, full reproducibility
across two runs of identical inputs, a legacy-vs-portfolio-risk
comparison, hand-calculable quantity/P&L and mark-to-market equity for
long/short/multi-symbol scenarios, risk-rejection auditability,
timeframe-agnosticism, and sequential portfolio-state visibility), plus
4 added to `tests/test_experiments.py`, 15 added to
`tests/test_analytics.py`, 1 added to `tests/test_ai_end_to_end.py`,
and 4 added to `tests/test_architecture.py`. Execution realism
(slippage, commissions, spread, partial fills, order-book simulation,
market impact, limit/stop-order execution) and advanced portfolio
management (Kelly sizing, VaR/CVaR, correlation optimization, factor
models, volatility targeting) remain explicitly out of scope -- this
sprint closes the risk-sizing gap between research and paper trading,
not the execution-realism gap. See `DECISIONS.md`, ADR-0044 for the
full design and reasoning.

**Sprint 10 (ML Signal Research & AI Strategy Integration) is complete
and confirmed via real `pytest` on the dev machine: 784 passed, 0
failed, all green** (scikit-learn/joblib aren't installed in the
sandbox used during development -- the same gap Streamlit had for
Sprint 9 -- so this sprint's sklearn-dependent tests only ran for real
once handed off to the dev machine). `src/ai/` is a
nine-module research-only ML capability: `features.py`
(`FeatureBuilder` -- 8 past-only columns built on the existing
`IndicatorEngine`, never a reimplemented indicator formula) and
`labels.py` (`LabelBuilder` -- a separately-configured 3-class
forward-return target, `LONG`/`SHORT`/`FLAT`) are deliberately
distinct classes, composed (never hidden inside each other) by
`dataset.py`'s `build_training_table()`. `splitting.py`'s
`chronological_split()` never shuffles and drops a `purge_bars`
embargo (always the label's own `horizon_bars`) at each internal
boundary so no training label's outcome window can reach into
validation or test -- proved directly in
`tests/test_ai_splitting.py`, not just asserted; `walk_forward_splits()`
gives a diagnostic expanding-window evaluator alongside the one
chronological holdout. `model.py`'s `AIModel` interface wraps a
`scikit-learn` `Pipeline(StandardScaler, LogisticRegression)` --
scaler fit only on the training split, schema-checked at inference
time (a missing/reordered/renamed feature fails loudly, never a
silently wrong prediction) -- behind a `register_model_type()` seam so
a second model implementation is "implement it, register it," not a
rewrite of `MLTrainingService`/`AISignalStrategy`. `identity.py`'s
`model_spec_id()` gives every trained model a deterministic identity
distinct from `artifacts.py`'s SHA-256 artifact content hash;
`registry.py`'s `ModelRegistry` (JSON metadata + joblib artifacts under
`data/models/`, no new SQLite schema) answers "which exact model
artifact produced this experiment" the same way `ExperimentRegistry`
answers it for experiments. `training.py`'s `MLTrainingService` is the
one application-facing entry point wiring the whole chain together --
`Backtester -> Analytics` and `evaluation.py`'s classification metrics
are kept strictly separate questions (model quality vs. trading
performance), never conflated into one number.

`src/strategies/ai_signal.py`'s `AISignalStrategy` is the entire seam
between `src.ai` and the rest of the platform: loads an already-
trained, frozen model by `model_id` at construction (never trains
again), maps predictions onto the existing `Signal`/`SignalDirection`
(no second, AI-specific decision model), emits sparsely (state-change
only, ADR-0015) with model provenance (`model_id`/`model_artifact_hash`/
`predicted_class`/`class_probability`/`feature_set_id`/
`label_horizon_bars`) in signal metadata, and reconstructs from
`ExperimentSpec`'s existing, unmodified `strategy_params` field alone
-- no `ExperimentSpec`/`ExperimentRegistry` schema change was needed
this sprint. `Backtester`/`src.risk`/`src.execution`/`src.portfolio`/
`src.dashboard`/`src.analytics` gained zero AI-specific code -- an
architecture test scans their actual source for exactly that, plus
confirms `src/ai` never imports `src.broker`/`src.execution`/
`src.risk`/`src.portfolio`/`src.dashboard`/`yfinance` directly. No LLM
anywhere in this chain (the AI Research Reporter's own separate LLM/
fallback narrative path, `src/research/`, is untouched). Sandbox suite
(scikit-learn/joblib unavailable there): 725 passed, same 2 known
environment-only failures, 8 skipped (5 whole test modules plus 2
gated assertions inside `tests/test_architecture.py`, all gated via
`pytest.importorskip`, the exact pattern Sprint 9 established for
Streamlit). **Real `pytest` on the dev machine (scikit-learn/joblib
installed): 784 passed, 0 failed** -- the 8 sandbox-skipped items ran
and passed for real, and the 2 sandbox-only environment failures
(Python-version check, config) don't reproduce on the dev machine's
own supported Python version. That first real run also surfaced two
test-authoring bugs (never production bugs), both fixed in a follow-up
commit: a Trade-equality check that compared randomly-generated
`Signal` UUIDs across two independent runs instead of trade economics,
and an architecture substring check that false-positived on
`AISignalStrategy`. See `DECISIONS.md`, ADR-0043 for the full design
and the exact list of what was gated and why.

**Sprint 9 (Analytics & Dashboard) is complete and confirmed via real
`pytest` on the dev machine: 688 passed, 0 failed, all green.**
`src/analytics/` computes deterministic backtest and portfolio
metrics from a backtest's `Trade` list and equity curve (now persisted
by `ExperimentRegistry`, not just held in memory) -- every metric is
either a real, full-precision number or an explicit `UNDEFINED` with a
stated reason, never a fabricated `0`/`inf`/`nan`. Sharpe and
volatility reuse Sprint 6's `infer_periods_per_year()` (ADR-0038)
rather than reinventing timeframe-aware annualization. `src/dashboard/`
is a read-only Streamlit app over that analytics layer -- it never
computes a metric itself, never bypasses `MarketDataService` for a
market price, and cannot submit an order, change a risk limit, or
mutate any `Portfolio`/`Experiment`/`Strategy` state
(`tests/test_architecture.py` enforces all three structurally). The
Paper Portfolio view is scoped to one experiment's own paper-executed
`Portfolio` at a time (per-experiment, not a new global paper-trading
account -- ADR-0021's `PaperTradingLoop` stays deferred future work),
marked to the *current* market price via a new
`PortfolioValuationService` -- closing the long-standing gap where
`PaperBroker`/`Portfolio` never marked an open position to market
(ADR-0022) without changing that platform behavior itself.

Getting to a clean real-`pytest` run took three rounds, each finding
and fixing a genuine issue in `tests/test_dashboard_smoke.py` --
never in production code, and each confirmed sandbox-reverified at 677
passed along the way: (1) a cross-test `@st.cache_data` cache-
poisoning bug (six tests reading a stale cached result from an
earlier test sharing the same relative db-path string), fixed by
clearing the cache in the `workdir` fixture; (2) an `AppTest` timeout
too tight (3s default) for a real analytics page once the cache fix
let a test actually reach it, fixed by raising every `.run()` call to
`timeout=15`; (3) the one that actually mattered -- a shared test
fixture (`RSI_CLOSES`) that a comment claimed left a position open,
which turned out false when checked directly against the file it
cited and confirmed empirically by running the real `run_experiment()`
pipeline: the position was fully closed by the end of that series'
recovery leg. Fixed with a new `OPEN_POSITION_CLOSES` series (a pure
decline -- `src.indicators.formulas.relative_strength_index`'s
`ewm`-based RSI pins at exactly 0 with zero gains ever, so the
strategy enters and never exits) that empirically leaves exactly one
open position, now used in the three tests that actually need one,
each with a sanity check and, for the position-rendering test, a real
assertion that the dataframe shows the open symbol. See
`DECISIONS.md`, ADR-0042 for the full account.

**Sprint 8 (Market Data Integrity & Session Awareness), including its
canonicalization-idempotence cleanup, is complete and confirmed via
real `pytest` on the dev machine.** Resolves ADR-0006 (data validation, proposed Sprint 1) and ADR-0007
(incremental cache, proposed Sprint 1), plus the timezone-policy
question ADR-0019 and ADR-0038 both referenced but left open --
internal candle timestamps are timezone-aware UTC everywhere now, a
full migration through `DataProvider`/`MarketDataService`'s contract
(confirmed explicitly before implementation, not scoped down).
`src/data/validation.py`'s `validate_candles()` rejects structurally
invalid or financially impossible data outright and flags gaps/
zero-volume as non-fatal `ValidationReport` entries -- session-aware
via the new `src/calendar/` package (`TradingCalendar`/`NYSECalendar`,
regular US equity session, holidays from a hand-rolled rule table, not
a calendar-library dependency -- also confirmed explicitly before
implementation). `src/data/canonical.py`'s `canonicalize_candles()` is
the single choke point both `dataframe_fingerprint()` and the cache now
run through, so a cache hit and a fresh fetch of identical data always
hash identically. The cache moved from exact-range keys to
`(symbol, interval)`-keyed incremental fetch. `get_candles()`/
`get_history()` kept their exact pre-Sprint-8 signatures; the new
`get_dataset()` returns the full `CandleDataset`, and
`Backtester.run()`'s new optional `dataset` parameter populates
`BacktestResult.dataset_identity` -- both additive, no existing call
site required a change. The real `pytest` run on the dev machine
(Python 3.14.6, pytest 9.1.1) actually returned **1 failed, 568
passed** -- an earlier version of this paragraph wrongly claimed "569
passed, 0 failed" before that run had been reported; that was a
documentation error, corrected here. The one real failure:
`test_canonicalize_is_idempotent` compared two canonicalization passes
whose `DatetimeIndex.freq` (a pandas-internal bookkeeping attribute,
not real data) differed (`<Day>` vs. `None`) across pandas versions,
even though values, dtypes, and content hash were all confirmed
identical. The fix is in production code, not the test: `canonicalize_
candles()` (`src/data/canonical.py`) now explicitly pins
`DatetimeIndex.freq` to `None` on its output, so this can't recur under
any pandas version. Three regression tests added (see `DECISIONS.md`,
ADR-0041 for the full list). Sandbox-reverified at **570 passed**
(567 + 3 new tests), same 2 known environment-only failures. Confirmed
via real `pytest` on the dev machine (Python 3.14.6): **572 passed, 0
failed, 0 errors, all green** -- both previously sandbox-only artifacts
passed for real, confirming they remain environment-only, not
regressions. Sprint 8's real-`pytest` confirmation of this fix is
complete -- up from the Pre-Sprint 7 baseline of 402 (470 after Sprint
7's initial implementation, 497 sandbox / 499 real after its cleanup,
567 sandbox for Sprint 8's initial implementation, 570 sandbox / 572
real after this cleanup).

**Sprint 7 (Portfolio-Aware Risk & Position Management), including its
ADR-0040 cleanup pass, is complete and confirmed via real `pytest` on
the dev machine.** `PortfolioRiskEngine` adds genuine, stop-based
position sizing plus portfolio-level exposure constraints alongside
(not replacing) `PositionSizer`'s allocation-only model;
`Portfolio`/`Position` (`src/portfolio/`) give the risk layer a
neutral, broker-independent view of what the platform currently holds;
`apply_fill_to_portfolio()` (`src/execution/portfolio_sync.py`) keeps
that view in sync with `PaperBroker`'s own simulation.
`PortfolioRiskEngine.decide_close()` (ADR-0040) is the formal,
symmetric close/exit-intent path, and `RiskDecision.capital_model`/
`to_trade_intent()` make the short-margin absence and the
Risk->Execution quantity invariant explicit and typed rather than
implicit. Confirmed via real `pytest` on the dev machine (Python
3.14.6, pytest 9.1.1): **499 passed in 1.18s**, all green -- including
both tests that failed only in the sandbox
(`test_python_version_passes_against_running_interpreter`,
`test_logger_is_importable_and_callable`), confirming those were
genuinely environment-only artifacts, not real regressions -- up from
the Pre-Sprint 7 baseline of 402 (470 after the initial Sprint 7
implementation, 497 sandbox-passed / 499 real-passed after this
cleanup). See `DECISIONS.md`, ADR-0039 and ADR-0040. Sprint 7 is
confirmed and ready to commit.

**Pre-Sprint 7: the research/strategy architecture is confirmed
timeframe-agnostic.** `calculate_metrics()`/`sharpe_ratio()` no longer
hardcode a daily annualization factor; `ExperimentSpec.interval` is now
a typed `Interval`; `scripts/run_experiment.py`'s `--interval` flag
keeps the fetched data and the recorded experiment timeframe in sync.
Confirmed via real `pytest` on the dev machine (Python 3.14.6):
**402 passed in 1.12s**, all green. This is architectural readiness
for daily/swing, intraday, and minute-scale research -- no live
intraday trading, tick feeds, or second-scale/HFT execution exist or
were built. See `DECISIONS.md`, ADR-0038. A subsequent formal
Acceptance Criteria document (AC-01 through AC-19) is closed with
`tests/test_intraday_acceptance.py` (9 tests) -- see the sprint
completion report for the full AC -> test mapping.

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

What was left on the Market Data Service as of Sprint 6 -- no data
validation beyond required-column checks (ADR-0006), whole-range
re-fetching on any cache-key miss (ADR-0007) -- is resolved by Sprint 8
(`DECISIONS.md`, ADR-0041; see the Current Module entry above). Still
outstanding, not addressed by Sprint 8 and not blocking it: caching
remains CSV, not Parquet; no integration test suite against the live
yfinance API; pre-market/after-hours session support is reserved
(`SessionPolicy`) but unimplemented.

## Next Task

Sprint 13 (AI Research Agent) is **implemented and sandbox-confirmed:
897 passed, 2 known environment-only failures, 13 skipped.** Two steps
remain, both requiring the operator's own dev machine (neither is
possible from this sandbox: no network, no `ANTHROPIC_API_KEY`):

1. **Real `pytest` confirmation.** From the repo root, with the
   project's `.venv` activated:

   ```bash
   source .venv/bin/activate
   pytest -q
   ```

   Expect a strictly better result than the sandbox's 897/2/13 --
   every scikit-learn/joblib/Streamlit-gated test (including this
   sprint's own new agent test files) should run for real and pass,
   and neither `test_python_version_passes_against_running_interpreter`
   nor `test_logger_is_importable_and_callable` should reproduce,
   exactly as every prior sprint's real run has confirmed. Do not
   assume a specific final count in advance -- report the actual
   number.

2. **Manual real-provider smoke test** (separate from the automated
   suite; Sprint 13 spec Phase 14 -- validates the agent system, not
   strategy profitability):

   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...   # never commit this
   python -m src.cli research-agent \
     --goal "Investigate whether the EMA strategy's drawdown is concentrated in volatile regimes"
   ```

   Confirm the agent: uses tools (visible in the "Tool activity"
   section of the CLI output), never references a forbidden capability,
   stops within its default budget (12 steps / 4 backtests), produces
   evidence-grounded observations, and distinguishes observation from
   hypothesis. A second good test goal: `"Compare the EMA and RSI
   strategies on the same symbol, timeframe, date range, risk
   configuration, and execution assumptions."` Do not treat this smoke
   test as statistical evidence about any strategy's quality -- it
   validates the agent, not the market.

Once both are done, update this file, `CHANGELOG.md`, and
`DECISIONS.md`'s ADR-0046 status with the real results (mirroring the
pattern every prior sprint's post-real-pytest doc update has followed),
commit, and push. Sprint 14+ planning (a Strategy Development Agent, a
Market Monitoring Agent, or a controlled trading agent) is otherwise
open, with no blocking work or open implementation question --
`ROADMAP.md`'s "Sprint 14+" section lists the explicitly deferred
candidates.

_Previously: Sprint 12 (Execution Realism & Transaction Cost Modeling) is
**complete and confirmed via real `pytest` on the dev machine: 916
passed, 0 failed** (3.92s). This is a strictly better result than the
sandbox's own 855 passed, 2 known failures, 8 skipped -- the dev
machine ran every scikit-learn/joblib/Streamlit-gated test the sandbox
could only skip (including the new cost-aware AI-compatibility test in
`tests/test_ai_end_to_end.py`), all passing, and neither sandbox-only
failure (`test_python_version_passes_against_running_interpreter`,
`test_logger_is_importable_and_callable`) reproduced, exactly as every
prior sprint predicted.

Sprint 11 (Portfolio-Aware Backtesting & Unified Risk Simulation) is
**complete and confirmed via real `pytest` on the dev machine: 850
passed, 0 failed** (Python 3.14.6, pytest 9.1.1, 4.57s). This is a
strictly better result than the sandbox's own 790 passed, 2 known
failures, 8 skipped -- the dev machine ran every scikit-learn/joblib-
gated AI module and the Streamlit-gated dashboard smoke module for
real (all passing), and neither sandbox-only failure
(`test_python_version_passes_against_running_interpreter`,
`test_logger_is_importable_and_callable`) reproduced, exactly as every
prior sprint predicted.

Sprint 10 (ML Signal Research & AI Strategy Integration) is complete
and **confirmed via real `pytest` on the dev machine: 784 passed, 0
failed, all green.** It took two real-pytest rounds to get there. The
sandbox suite (scikit-learn/joblib unavailable there, the same gap
Streamlit had for Sprint 9) had shown 725 passed, 2 known
environment-only failures, 8 skipped -- every test needing a real
model fit (`tests/test_ai_model.py`, `test_ai_registry.py`,
`test_ai_training.py`, `test_ai_signal_strategy.py`,
`test_ai_end_to_end.py`, plus 2 gated assertions inside
`test_architecture.py`). Run 1 on the dev machine (782 passed, 2
failed) surfaced two genuine test-authoring bugs the sandbox review
couldn't catch without a real scikit-learn: (1)
`test_repeated_backtests_against_the_same_frozen_model_are_identical`
compared full `Trade` dataclasses, including `entry_signal_id`/
`exit_signal_id` -- but `Signal.id` is `field(default_factory=uuid4)`
(`src/signals/models.py`), so two independent runs mint fresh random
ids for otherwise-identical signals even with a fully deterministic
model; fixed by comparing trade economics (entry/exit time, direction,
entry/exit price) instead of raw dataclass equality; (2) the
`test_ai_signal_strategy_emits_canonical_signal_objects` architecture
guard used `"class AISignal" not in text`, a plain substring check
that also matches inside the real, intended `class AISignalStrategy`
-- a guaranteed false positive; fixed with a word-boundary regex.
Neither was a production-code bug -- `src/ai`/`src/strategies/
ai_signal.py` were correct as designed. Run 2 confirmed **784 passed,
0 failed** -- notably the sandbox's 2 "known environment-only"
failures (Python-version check, config) don't reproduce on the dev
machine's own supported Python version either, so this run has zero
failures of any kind. No `src/backtesting`/`src/risk`/`src/execution`/
`src/portfolio`/`src/dashboard`/`src/analytics` production code
changed this sprint; see `DECISIONS.md`, ADR-0043 for the full
account. Sprint 11+ (LLM-based reasoning as a second AI signal
approach, an AI-specific dashboard page) is future work, not started.

Sprint 9 (Analytics & Dashboard) is implemented, committed
(`aeca0c8`, `38e693b`, `e9ff798`, plus a final docs-only commit for
this confirmation), and now **confirmed via real `pytest` on the dev
machine: 688 passed, 0 failed, all green.** Getting there took three
real `pytest` rounds on the dev machine, each finding and fixing a
`tests/test_dashboard_smoke.py` issue -- the one file this sandbox
could only ever skip, never execute. Run 1: **682 passed, 6 failed**,
a `@st.cache_data` cross-test cache-poisoning bug; fixed by clearing
the cache in the `workdir` fixture. Run 2: **686 passed, 2 failed** --
a genuine `AppTest` timeout (fixed by raising every `.run()` call to
`timeout=15`) and a not-yet-diagnosed warning-rendering failure,
addressed defensively with an extra cache-clear plus a richer
diagnostic assertion. Run 3: **687 passed, 1 failed** -- that
diagnostic assertion did its job: it showed the portfolio had *zero*
open positions, not one as three tests' shared fixture data
(`RSI_CLOSES`) and a comment assumed -- checked directly against the
file the comment cited and found false.
`RSIMeanReversionStrategy` had, in fact, closed the position by the
end of that specific series' recovery leg. Fixed with a new
`OPEN_POSITION_CLOSES` series (the decline only, no recovery -- RSI
pinned at exactly 0 the whole way per the real `ewm`-based formula),
empirically confirmed via the real `run_experiment()` pipeline to
leave exactly one open `QQQ` position. The three tests that actually
need an open position now use it, each gained a
`len(portfolio.positions) == 1` sanity check, and the "with an open
position" test gained a real assertion that the per-position
dataframe renders `"QQQ"` -- a code path never actually exercised
before this fix. No `src/dashboard`/`src/analytics` production code
changed in any of the three fixes; see `DECISIONS.md`, ADR-0042 for
the full account. Sandbox-reverified at 677 passed each round,
unchanged. Run 4 -- the confirming run -- returned **688 passed, 0
failed**: sandbox's 677 plus the 9 previously-skipped
`test_dashboard_smoke.py` tests, and the 2 previously-known
sandbox-only failures (cli/doctor, config) also passed for real, as
expected (they were always environment-only, never product bugs).
Sprint 9 is genuinely complete and verified. Only `git push origin
main` remains, and that has to run from the user's own machine (this
sandbox has no outbound network access). AI/ML signal generation
(`ROADMAP.md`'s other Sprint 9 candidate, now Sprint 10) remains
explicit future work, not started.

Sprint 8 (Market Data Integrity & Session Awareness) is implemented,
committed (`53db66c`), and its follow-up canonicalization-idempotence
cleanup is confirmed via real `pytest` on the dev machine (Python
3.14.6): **572 passed, 0 failed, 0 errors, all green**. (For the
record: the sprint's first real-`pytest` run returned 1 failed, 568
passed, not the "569 passed, 0 failed" this section briefly and
wrongly claimed before that run had actually happened; see
`DECISIONS.md`, ADR-0041 for the root cause, fix, and full timeline.)
Separately available, none yet explicitly requested: setting
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
  lunch/power hour) -- previously blocked on candle timestamps
  reliably being in market-local time (ADR-0006), which Sprint 8
  (`DECISIONS.md`, ADR-0041) now provides via `src/calendar`'s
  `TradingCalendar.session_date()`. The blocker is gone; the
  attribution feature itself is still not built -- natural next step,
  not done this round. The AI Research Reporter inherits this gap too
  -- it cannot yet suggest anything session-related, only
  regime-related.
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
  distance -- true risk-based (stop-loss-distance) sizing is now built,
  as of Sprint 7, in a separate, standalone engine
  (`PortfolioRiskEngine`, `src/risk/portfolio_risk.py`, `DECISIONS.md`
  ADR-0039) rather than by changing what `PositionSizer`/
  `allocation_per_trade_pct` mean. `PortfolioRiskEngine` is likewise not
  wired into `Backtester` -- see the new Sprint 7 debt entries below.
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
- `infer_periods_per_year()` (`DECISIONS.md`, ADR-0038) is a
  calendar-time approximation, not an exchange-session-aware one -- it
  doesn't know NYSE hours, market holidays, or that some markets trade
  24/7, so intraday Sharpe annualization is order-of-magnitude-correct,
  not precise. A future, session-aware version is real future work, not
  required to stop annualization from being silently wrong the way the
  flat `252` default was. Relatedly, no live intraday data has actually
  been run through `scripts/run_experiment.py` against a real provider
  yet -- only through synthetic daily/1-minute fixtures in
  `tests/test_timeframe_agnostic.py` -- so intraday support is
  confirmed architecturally, not yet exercised end to end against real
  market data.
- `PortfolioRiskEngine`/`Portfolio` (Sprint 7, `DECISIONS.md` ADR-0039)
  don't support position scaling (adding to or partially reducing an
  existing position) -- a same-symbol call is rejected outright
  (`POSITION_SCALING_NOT_SUPPORTED`), matching `PaperBroker`'s existing
  one-open-position-per-symbol limitation. Nor do they implement Kelly
  criterion sizing, VaR/CVaR, correlation-aware exposure, portfolio
  optimization, factor models, volatility targeting, dynamic hedging,
  sophisticated margin modeling, market-impact modeling, order-book
  simulation, or ML-based risk models -- all explicitly out of scope
  for Sprint 7. `Portfolio`/`Position.current_price`/`unrealized_pnl`
  exist but nothing sets `current_price` automatically -- no real
  mark-to-market, the same limitation `PaperBroker.account_state` has
  always had (ADR-0022). Like `PositionSizer` before it,
  `PortfolioRiskEngine` composes with `PaperBroker`/`Portfolio` only via
  tests (`tests/test_sprint7_integration.py`) -- `Backtester.run()`
  itself still sizes every trade as a single unit (ADR-0011).
- The ADR-0040 cleanup formalized the close path
  (`PortfolioRiskEngine.decide_close()`) but did not add partial-close
  support -- a `quantity` other than a held position's full size is
  rejected with `RejectionReason.UNSUPPORTED_POSITION_OPERATION`,
  exactly like a same-symbol scaling attempt through `decide()`. No
  `ExecutionResult` wrapper type was introduced either: `Fill`
  (execution) and `Portfolio`/`Position` (state) already are the
  distinct structured outputs of those layers, and `PaperBroker.
  submit_signal()` still reports failure by raising, unchanged --
  adding a parallel result type with no actual consumer was judged
  speculative and out of this cleanup's scope (`DECISIONS.md`,
  ADR-0040). `ApprovedTradeIntent`/`to_trade_intent()` exist as an
  explicit audit/boundary object but are not wired into `PaperBroker`
  -- `as_sizing_decision()` remains what `PaperBroker` actually
  consumes.
- Sprint 8 (`DECISIONS.md`, ADR-0041) doesn't model pre-market/
  after-hours sessions -- `SessionPolicy` reserves the concept
  (`REGULAR`/`ALL` only), and `NYSECalendar` doesn't model NYSE's rare
  unscheduled closures (a national day of mourning, say) or extend past
  `SUPPORTED_YEARS` (2015-2035) without deliberately re-verifying and
  widening it. Gap detection isn't built for weekly/monthly bars --
  how many weekly bars *should* exist across a holiday-shortened week
  isn't well-defined without more calendar work than this round
  scoped. Incremental cache fetch only extends a cached span's upper
  bound; a request whose `start` moves earlier falls back to a full
  refetch rather than fetching just the missing prefix. `get_dataset()`
  exists alongside `get_candles()`/`get_history()` but nothing in the
  codebase has been migrated to prefer it yet -- `ExperimentSpec.capture()`
  and `Backtester.run()`'s new `dataset` parameter both still take a
  plain DataFrame/optional dataset, additively, not a required one.
- Sprint 9's Paper Portfolio view (`DECISIONS.md`, ADR-0042) is scoped
  strictly to the platform's own `PaperBroker`/`Portfolio` path for one
  experiment at a time -- real broker P&L (Alpaca/IBKR/IG/Tiger) is not
  wired into the dashboard, and there is still no global, standing
  paper-trading account (`ADR-0021`'s `PaperTradingLoop` remains
  deferred future work; Sprint 9 deliberately did not build it, per
  explicit `AskUserQuestion` confirmation choosing the per-experiment
  scope instead). `PortfolioValuationService` marks a saved snapshot to
  the *current* market price on read, but never re-simulates a trade --
  `Portfolio`/`PaperBroker` themselves still don't mark to market
  automatically (ADR-0022 unchanged). The Overview page caps its
  per-experiment analytics table to the most recent 20 experiments (a
  guard against recomputing metrics for every experiment ever logged,
  not real pagination) -- a natural next step if the registry grows
  large enough for that to matter. `src.analytics.metrics.exposure_time()`/
  `sharpe_ratio()`/`volatility()` inherit `infer_periods_per_year()`'s
  existing calendar-time (not exchange-session-aware) approximation
  from ADR-0038 -- unchanged, not revisited this round.

## How to verify this file is accurate

```bash
pytest                    # Sprint 9 baseline: 688 passed, confirmed on the
                          # real dev machine (21 architecture + 8 attribution
                          # + 12 backtesting + 34 broker + 6 cache + 19 calendar
                          # + 29 cli/doctor + 7 config + 15 data_canonical
                          # + 24 data_validation + 59 analytics
                          # + 16 dashboard_formatting + 9 dashboard_smoke
                          # + 11 ema_cross_strategy + 19 execution
                          # + 12 experiment_spec + 33 experiments + 6 hashing
                          # + 15 ibkr + 31 ig + 11 indicators
                          # + 6 integration_paper_trading + 9 intraday_acceptance
                          # + 27 market_data + 3 pipeline_contract + 5 portfolio
                          # + 27 portfolio_position + 58 portfolio_risk
                          # + 13 portfolio_valuation + 11 reconciliation
                          # + 10 regime + 17 research + 17 risk
                          # + 14 rsi_mean_reversion_strategy + 9 run_experiment_script
                          # + 13 signals + 10 sprint7_integration + 9 strategy_registry
                          # + 13 strategy_sdk + 15 tiger + 6 timeframe_agnostic).
                          #
                          # Sprint 10 adds 96 more test functions: 10
                          # ai_features + 11 ai_labels + 7 ai_dataset + 15
                          # ai_splitting (all network-free and scikit-learn-
                          # free) + 7 new architecture assertions (28 total,
                          # up from 21) + 46 gated on scikit-learn/joblib
                          # (12 ai_model + 12 ai_registry + 9 ai_training +
                          # 10 ai_signal_strategy + 3 ai_end_to_end, plus 2 of
                          # the 7 new architecture assertions).
                          # **Confirmed via real pytest on the dev machine:
                          # 784 passed, 0 failed, all green** (688 + 96) --
                          # even the 2 previously "known environment-only"
                          # failures (cli/doctor Python-version check, config)
                          # don't reproduce there, since the dev machine runs
                          # its own actually-supported Python version. This
                          # sandbox has neither scikit-learn nor joblib
                          # installed and no network access to add them -- its
                          # own stub-based runner shows 725 passed, 2 known
                          # environment-only failures (cli/doctor 28/29,
                          # config 6/7, unchanged), 8 skipped (5 whole AI test
                          # modules + 2 gated architecture assertions,
                          # scikit-learn-gated via pytest.importorskip -- the
                          # exact pattern Sprint 9 used for dashboard_smoke's
                          # Streamlit gate; see DECISIONS.md, ADR-0043). The
                          # first real-pytest round found 2 failures -- both
                          # test-authoring bugs the sandbox couldn't catch
                          # without a real scikit-learn (a Trade-equality
                          # check comparing random Signal UUIDs, and an
                          # architecture substring check that false-positived
                          # on AISignalStrategy) -- fixed, then reconfirmed at
                          # 784 passed, 0 failed on the second round.
python src/main.py        # should log startup + watchlist
python -m src.cli doctor  # should print one line per check and end with "Everything Healthy"
                          # (Broker Connection shows NOT_IMPLEMENTED until
                          # ALPACA_API_KEY/ALPACA_API_SECRET are set)
streamlit run src/dashboard/app.py
                          # should open the read-only research dashboard;
                          # populate data first via
                          # `python scripts/run_experiment.py` (see "Sprint 9"
                          # above), otherwise every page shows "No experiments
                          # found yet"
```

Confirmed via the sandbox stub-based test runner
(`PYTHONPATH=/tmp/stubs:. python3 /tmp/runner_all.py`): **677 passed**,
2 known environment-only failures --
`test_python_version_passes_against_running_interpreter` (sandbox
Python 3.10 vs. the dev machine's pinned newer version) and
`test_logger_is_importable_and_callable` (the sandbox's `loguru` stub
is a no-op and doesn't write to stdout the way real `loguru` does) --
plus 1 module correctly skipped, `tests/test_dashboard_smoke.py` (its
own `pytest.importorskip("streamlit")` skips it cleanly since this
sandbox has no network access to install Streamlit; it's pinned in
`requirements.txt` and is expected to run for real, and pass, on the
dev machine). **Sprint 9's real-`pytest` confirmation is pending** --
report back the actual dev-machine total once run, per the project's
standing verification process; do not treat the "~688" estimate above
as a substitute for that.

**Correction (Sprint 9):** the first real `pytest` run on the dev
machine returned **682 passed, 6 failed**, not the "~688 passed"
estimate above assumed -- all 6 failures inside
`tests/test_dashboard_smoke.py`, the one module this sandbox could
only ever skip, never execute. Cause: that file's `@st.cache_data`-
backed loaders cache purely on the relative `db_path` string
(`"data/experiments.db"`), which every test in the file shares even
though each `chdir`s into its own `tmp_path` -- the whole file runs in
seconds, well inside the 30-60s cache TTL, so the first test's
(empty) result silently served every later test too, routing them all
into the "no experiments" branch regardless of what they had actually
populated. A test-isolation bug only -- a real deployment has exactly
one `data/experiments.db`, so this collision can't occur outside a
test suite reusing that string across many temp directories. Fixed by
calling `st.cache_data.clear()` in the `workdir` fixture; no
`src/dashboard`/`src/analytics` code changed. Sandbox-reverified at
677 passed, unchanged. A second real-`pytest` run (expected 688
passed) is pending -- see `DECISIONS.md`, ADR-0042 for the full
account.

**Second correction (Sprint 9):** the second real `pytest` run
returned **686 passed, 2 failed** -- confirming the cache fix above
resolved four of six original failures. Two remained, both again in
`tests/test_dashboard_smoke.py`: (1)
`test_app_runs_experiment_analysis_page` hit `AppTest`'s default 3s
per-`.run()` timeout, now that the cache fix let it reach a page that
genuinely computes analytics and draws several charts -- fixed by
raising every `.run()` call in the file to `timeout=15`, a test-tooling
change, not a production performance issue (`AppTest` re-executes the
whole script cold on every call; a real Streamlit server doesn't). (2)
`test_paper_portfolio_page_degrades_gracefully_when_price_unavailable`
still saw zero warnings, and its exact mechanism could not be fully
confirmed from static reading of `src/dashboard/views.py`/
`src/analytics/valuation.py` alone -- every individual piece is
independently unit-tested and passing. The one identified gap: this
test switches `MarketDataService.get_history` from success to
`NoDataError` mid-test, and `_cached_latest_price`/`_value_portfolio`'s
60s TTL means a stale, previously-successful price could in principle
still be served. Addressed with a defensive extra
`st.cache_data.clear()` right after the failure monkeypatch and a
richer assertion (dumps `at.error`/`at.info`/`at.metric` on failure)
so a third occurrence would be conclusive. Sandbox-reverified at 677
passed, unchanged. A third real-`pytest` run (still expected 688
passed) is pending -- see `DECISIONS.md`, ADR-0042 for the full
account.

**Third correction (Sprint 9) -- the actual root cause:** the third
real `pytest` run returned **687 passed, 1 failed**, and the enriched
assertion from the second correction printed exactly the missing
piece: `info=['No open positions.']`. The portfolio had zero open
positions, not one -- `RSI_CLOSES`'s recovery leg was strong enough
that `RSIMeanReversionStrategy` (period 5) exited back to FLAT before
the series ended, closing the position it opened during the decline.
A comment on the failing test (and two siblings) had claimed this was
"proven already by `tests/test_run_experiment_script.py`'s sibling
assertions" -- checked directly against that file and found false; it
never asserts anything about open positions for this combination.
Neither of the first two corrections above was ever the actual cause:
`PortfolioValuationService.value()` correctly never had a position to
price, so it correctly never had a reason to warn. Fixed with a new
`OPEN_POSITION_CLOSES` constant -- the decline only, no recovery leg
-- since `src.indicators.formulas.relative_strength_index`'s
`ewm`-based RSI is pinned at exactly 0 for a series with zero gains
ever, keeping the strategy oversold (and therefore LONG and never
exiting) for the whole series. Verified empirically, not just
reasoned through, by running the real `run_experiment()` pipeline
directly against this series: 1 trade, 1 open `QQQ` position (entry
`$98.0`, quantity `~102.04`), 0 closed. The three tests that actually
need an open position now use `OPEN_POSITION_CLOSES`, each gained a
`len(portfolio.positions) == 1` sanity check before the `AppTest` even
runs, and the "with an open position" test gained a real assertion
that the per-position dataframe renders `"QQQ"` -- exercising a code
path this file had never actually covered before. Sandbox-reverified
at 677 passed, unchanged. **Confirmed via real `pytest` on the dev
machine: 688 passed, 0 failed, all green** -- the fourth real run, and
the first one this sprint's own test suite actually passed outright.
Sprint 9 (Analytics & Dashboard) is now genuinely verified, not just
implemented or sandbox-verified.

**Correction (Sprint 8):** an earlier version of this section claimed
"Confirmed via real `pytest` on the dev machine: 569 passed, 0 failed,
0 errors"
before that run had actually been reported -- the real run in fact
returned **1 failed, 568 passed**. The one failure:
`test_data_canonical.test_canonicalize_is_idempotent` compared two
canonicalization passes' `DatetimeIndex.freq` (pandas bookkeeping, not
real data, and not part of what `dataframe_fingerprint()` hashes),
which differed (`<Day>` vs. `None`) across pandas versions even though
values/dtypes/hash were all confirmed identical. That was fixed first
by relaxing the test (`check_freq=False`); on review, that fix was
reverted as insufficient -- letting `.freq` differ between two
"canonical" outputs, even just in a test comparison, undercuts the
guarantee `canonicalize_candles()` exists to provide. The real fix is
in `src/data/canonical.py`: `canonicalize_candles()` now explicitly
pins `DatetimeIndex.freq` to `None` on its output, so this can't recur
under any pandas version. Three regression tests were added (see
`DECISIONS.md`, ADR-0041, for the full list), bringing the sandbox
total from 567 to 570. **Confirmed via real `pytest` on the dev
machine: 572 passed, 0 failed, 0 errors, all green** (570 sandbox +
both previously sandbox-only artifacts passing for real). Sprint 8's
real-`pytest` confirmation of this fix is complete.

_Historical: Sprint 7 (including its ADR-0040 cleanup) was confirmed
via real `pytest` on the dev machine (Python 3.14.6, pytest 9.1.1):
**499 passed in 1.18s, all green** -- both sandbox-only artifacts
passed for real, confirming they were environment-only, not
regressions. Sandbox-confirmed at 470 passed immediately after the
initial Sprint 7 implementation (ADR-0039), before the ADR-0040
cleanup added 27 more tests. Confirmed via real `pytest` on the dev
machine (Python 3.14.6, pytest 9.1.1) as of the Pre-Sprint 7 entry:
**402 passed in 1.12s**, all green. As of the prior (Sprint 6 part 2)
entry: 386 passed in 0.92s, all green._
