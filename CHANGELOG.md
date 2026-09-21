# Changelog

All notable changes to this project are documented here, grouped by
sprint. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

**Extension Cost convention (see `DECISIONS.md`, ADR-0014):** any entry
that adds a new indicator, strategy, broker, or data vendor includes an
`Extension Cost: N file(s) changed: <list>` line. This isn't a target
to hit -- it's a running record so we stay aware of how many existing
files a new feature actually touches. A couple of files is normal;
touching a large share of the codebase for one addition is the real
warning sign that the architecture's been violated.

## Sprint 9 -- 2026-09-21, Analytics & Dashboard

A read-only Streamlit research and paper-portfolio analytics interface
over the platform's existing backtesting, experiment, strategy,
market-data, and portfolio capabilities. Resolves the roadmap
ambiguity between "Analytics & Dashboard" and "AI" as Sprint 9
candidates in favor of the former; AI/ML signal generation remains
explicit future work. See `DECISIONS.md`, ADR-0042 for the full design
and reasoning.

### Added

- `src/analytics/` (new top-level package) -- deterministic backtest
  and portfolio analytics, zero Streamlit dependency.
  - `models.py`: `Metric`/`MetricStatus` (every computed number is
    explicitly `OK` or `UNDEFINED`, with a reason, never a silent `0`/
    `inf`/`nan`), `BacktestAnalytics` (metrics plus full identity/
    provenance), `ComparisonResult`/`ComparisonWarning`,
    `PositionValuation`/`PortfolioSnapshot`.
  - `metrics.py`: pure functions over `list[Trade]` + `pd.Series`
    equity curves -- `total_pnl`, `total_return`, `win_rate`,
    `profit_factor`, `expectancy`, `average_winner`/`average_loser`,
    `largest_winner`/`largest_loser`, `winning_trade_count`/
    `losing_trade_count`, `max_drawdown`, `drawdown_curve`,
    `sharpe_ratio`, `volatility`, `exposure_time`. Sharpe/volatility
    reuse `src.backtesting.metrics.infer_periods_per_year` (ADR-0038)
    for timeframe-aware annualization and return the exact
    `periods_per_year` used.
  - `service.py`: `AnalyticsService.analyze_backtest()`/
    `analyze_experiment()` and `compare_experiments()` -- the single
    place these metric definitions are wired together; never a
    composite "best strategy" score.
  - `valuation.py`: `PortfolioValuationService.value()` (strictly
    read-only mark-to-market valuation of a `Portfolio` snapshot),
    `latest_price()`/`default_price_lookup()` (the only sanctioned
    `MarketDataService` touchpoint in this package or the dashboard).
- `src/dashboard/` (new top-level package) -- the Streamlit app
  (`streamlit run src/dashboard/app.py`).
  - `app.py`: entrypoint, sidebar page routing (Overview, Backtest/
    Experiment Analysis, Strategy Comparison, Paper Portfolio) and
    experiment/comparison selectors.
  - `views.py`: one `render_*` function per page -- every number comes
    from `src.analytics`; the dashboard never computes a metric itself.
  - `formatting.py`: pure, Streamlit-free presentation formatting
    (`format_metric`/`format_number`/`format_interval`/etc.) --
    rounding/display belongs only here, never in `src.analytics`.
- `src/experiments/registry.py` -- four new additive tables and eight
  new methods: `save_trades()`/`get_trades()`,
  `save_equity_curve()`/`get_equity_curve()`,
  `save_portfolio()`/`get_portfolio()`,
  `save_research_report()`/`get_research_report()`. Written once by
  `scripts/run_experiment.py` immediately after `log_experiment()`
  returns an id; every accessor returns an explicit "nothing here"
  value (`[]`, an empty `pd.Series`, or `None`) for an experiment
  logged before Sprint 9, rather than fabricating one.
- `src/portfolio/models.py` -- `Portfolio.reconstruct(cash, positions,
  closed_positions)`: a new classmethod for restoring an
  already-known, previously-saved `Portfolio` snapshot, distinct from
  the fill-simulating `open_position()`/`close_position()`.
- `scripts/run_experiment.py` -- `_fill_signals_through_risk_and_execution()`
  now also applies every `Fill` to a neutral `Portfolio`
  (`src.execution.portfolio_sync.apply_fill_to_portfolio`, Sprint 7)
  alongside the existing `PaperBroker`; `run_experiment()` persists
  trades/equity curve/portfolio/research report and
  `ExperimentRunResult` gained a `portfolio` field.

### Tests

- `tests/test_analytics.py` (59) -- `Metric`/`MetricStatus`, every
  metric function's hand-calculated edge cases (empty/one/all-winning/
  all-losing/mixed/zero-P&L/zero-variance/flat-curve), timeframe/
  annualization, `AnalyticsService`, `compare_experiments()`.
- `tests/test_portfolio_valuation.py` (13) -- long/short/multiple
  positions, missing-price aggregation, realized-P&L independence,
  read-only verification, `latest_price()`/`default_price_lookup()`.
- `tests/test_dashboard_formatting.py` (16) -- unconditional, no
  Streamlit dependency to skip on.
- `tests/test_dashboard_smoke.py` (9) -- gated with
  `pytest.importorskip("streamlit")`, uses
  `streamlit.testing.v1.AppTest`, fully network-free via a
  class-level `MarketDataService.get_history` monkeypatch.
- `tests/test_experiments.py` (+12), `tests/test_run_experiment_script.py`
  (+2) -- the new registry persistence, round-tripped and scoped
  per-experiment.
- `tests/test_architecture.py` (+8) -- analytics does not depend on
  dashboard or Streamlit; dashboard depends on analytics; no circular
  dependency; dashboard never imports yfinance or reads the market-data
  cache directly; `MarketDataService` is constructed only inside
  `src.analytics.valuation`.

### Notes

- Explicitly not built this round: AI/ML signal generation, LLM
  trading decisions, AI agents or strategy selection; order placement
  or broker execution from the dashboard; a global standing
  paper-trading account (`PaperTradingLoop`, already deferred);
  real-broker P&L in the dashboard; execution-realism/slippage/
  commission simulation; tick/order-book data; auth/user management;
  cloud deployment; a mobile UI; portfolio optimization; new strategy
  families.
### Verified

- Sandbox-verified: 677 passed, same 2 known environment-only
  artifacts (`test_cli_doctor`'s Python-version check,
  `test_config`'s logger-stdout check), plus 1 correctly skipped
  (`test_dashboard_smoke.py`, no Streamlit installed in the sandbox --
  expected to run for real on the dev machine, where
  `streamlit==1.59.2` is already pinned in `requirements.txt`).
- **Correction:** the real `pytest` run on the dev machine (Python
  3.14.6, pytest 9.1.1) returned **6 failed, 682 passed** -- every
  failure inside `tests/test_dashboard_smoke.py`, the one file this
  sandbox could only ever skip, never actually execute. Root cause:
  `src/dashboard/views.py`'s `@st.cache_data`-decorated loaders key
  their cache purely on the `db_path` string argument
  (`"data/experiments.db"`), which every test in the file shares --
  each pointing at a different `tmp_path` after `chdir`, but
  indistinguishable to Streamlit's cache, and the whole file runs in
  ~3 seconds, well inside the loaders' 30-60s TTL. The first test's
  (genuinely empty) result silently served every later test too,
  routing them all into `app.py`'s "No experiments found yet" branch
  regardless of what they had actually populated -- explaining all six
  failures (missing headers, an unrendered multiselect, wrong info
  text, zero warnings shown). A test-isolation bug only: a real
  deployment has exactly one `data/experiments.db`, so this collision
  cannot occur outside a test suite reusing that same relative string
  across many temp directories in quick succession.
- Fix: the `workdir` fixture (`tests/test_dashboard_smoke.py`) now
  calls `st.cache_data.clear()` immediately after `chdir`, so every
  test starts with a cold cache. No `src/dashboard`/`src/analytics`
  production code changed.
- Sandbox-reverified after the fix: 677 passed, same 2 known
  environment-only artifacts, `test_dashboard_smoke.py` still
  correctly skipped in-sandbox (the fix itself can only be confirmed
  by a second real run, since this sandbox has no Streamlit
  installed). Real-`pytest` reconfirmation on the dev machine
  (expected 688 passed) is pending -- see `DECISIONS.md`, ADR-0042 for
  the full account.

## Sprint 8 -- 2026-09-16, Market Data Integrity & Session Awareness

Makes market data trustworthy and reproducible enough for daily and
minute-level research and trading: validated, timestamp-correct,
session-normalized, identified, and cached before it reaches
strategies/backtests. Resolves two long-deferred ADRs (ADR-0006 data
validation, ADR-0007 incremental cache) and the timezone-policy
question referenced but left open by ADR-0019 and ADR-0038. See
`DECISIONS.md`, ADR-0041 for the full design and reasoning.

### Added

- `src/calendar/` (new top-level package) -- `TradingCalendar(ABC)`
  (`is_session`, `session_open`/`session_close`, `sessions_between`,
  `session_date`) and `NYSECalendar`, the first (and only) concrete
  implementation: regular US equity session (09:30-16:00
  America/New_York), holidays computed from a small, explicit,
  rule-based table (`_nyse_holidays()`) rather than a hardcoded date
  list or a calendar-library dependency, verified for
  `SUPPORTED_YEARS` 2015-2035.
- `src/data/canonical.py` (new) -- `canonicalize_candles()`: pins row
  order, column order, numeric dtype (float64), and timezone (UTC) on
  any candle DataFrame. The single choke point everything hashed via
  `dataframe_fingerprint()` and everything cached now passes through
  first.
- `src/data/validation.py` (new) -- `validate_candles()`: structural
  checks (missing/duplicate/unordered timestamps, naive index, missing
  columns/values, empty symbol) raise `StructuralValidationError`;
  financial-sanity checks (`high < max(open, close)`, `low >
  min(open, close)`, `high < low`, negative volume) raise
  `FinancialSanityError`; gap detection (session-aware, given a
  `TradingCalendar`) and zero-volume flags are non-fatal, recorded on
  the returned `ValidationReport`.
- `src/data/models.py` (new) -- `SessionPolicy` (`REGULAR`/`ALL`),
  `GapReport`, `ValidationReport`, `DatasetIdentity`, and
  `CandleDataset` -- the "Canonical Dataset" everything downstream
  should consume: candles plus symbol/interval/provider/timezone/
  session-policy/requested-range/dataset-range/content-hash/
  validation-report.
- `src/data/exceptions.py` -- `DataValidationError` (base),
  `StructuralValidationError`, `FinancialSanityError`.
- `src/data/service.py` -- `MarketDataService.get_dataset()` (new,
  additive alongside the unchanged `get_candles()`/`get_history()`
  signatures): returns a full `CandleDataset`. Internally reworked:
  candles are canonicalized and validated on every return path (fresh
  fetch or cache hit); the cache key moved from `(symbol, interval,
  start, end)` to `(symbol, interval)`, storing the widest span fetched
  so far and fetching only the new tail when a request's `end` extends
  past it (falling back to a full refetch when a request's `start`
  moves earlier); a new `session: SessionPolicy` parameter (`REGULAR`
  default) filters intraday candles to the regular session, a no-op
  for daily+ intervals.
- `src/data/base.py` -- `DataProvider.fetch_candles()`'s contract
  loosened: a provider may return either a timezone-naive index
  (treated as already UTC) or a timezone-aware one in its own vendor
  convention; `MarketDataService` canonicalizes to UTC uniformly
  either way.
- `src/data/yfinance_provider.py` -- `_normalize()` no longer strips
  timezone information from yfinance's response (it used to discard
  the tz-aware intraday timestamps yfinance actually returns).
- `src/backtesting/models.py` -- `BacktestResult.dataset_identity:
  DatasetIdentity | None = None` (new, default `None` -- backward
  compatible).
- `src/backtesting/engine.py` -- `Backtester.run()` gained an optional
  `dataset: CandleDataset | None` parameter to populate
  `result.dataset_identity`; omitted, behavior is identical to before
  this parameter existed.
- `tests/test_calendar.py` (19 tests), `tests/test_data_validation.py`
  (24 tests), `tests/test_data_canonical.py` (13 tests) -- new files.
  11 tests added to `tests/test_market_data.py` (tz-aware output,
  `get_dataset()`, incremental fetch's three branches, session
  filtering's three cases, cache/fresh-fetch hash equivalence). 3 tests
  added to `tests/test_architecture.py` (no raw-provider access outside
  `src/data`, `src/calendar`'s leaf-dependency status).

### Decided

- Timezone policy resolved: internal candle timestamps are
  timezone-aware UTC, always -- a full migration through
  `DataProvider`/`MarketDataService`'s return contract, not scoped down
  to new surfaces only, confirmed explicitly before implementation. The
  actual test-fixture blast radius was one file
  (`tests/test_market_data.py`), far smaller than the ~15-25-file
  pre-implementation estimate, since every other existing test builds
  candles directly and never routes through `MarketDataService`.
- NYSE holidays: a hand-rolled, rule-based table, not a calendar-library
  dependency -- confirmed explicitly before implementation, trading a
  documented inability to model unscheduled closures for zero new
  dependencies and no per-year maintenance.
- Daily-bar gap detection compares raw UTC dates, not exchange-local
  dates -- converting a date-only daily timestamp to exchange-local
  time before taking `.date()` shifts it to the previous calendar day
  for any exchange behind UTC, which would have produced spurious gaps
  at month/holiday boundaries. Intraday gap detection does convert to
  exchange-local time first, correctly, since those timestamps are real
  instants.
- `get_candles()`/`get_history()` keep their exact pre-Sprint-8
  signatures; `get_dataset()` is new and additive. `Backtester.run()`'s
  new `dataset` parameter is optional and additive. Nothing that
  already called either had to change.

### Verified

- Sandbox-verified (`PYTHONPATH=/tmp/stubs:. python3 /tmp/runner_all.py`):
  **567 passed**, 2 known environment-only failures
  (`test_python_version_passes_against_running_interpreter`,
  `test_logger_is_importable_and_callable`) -- up from ADR-0040's 497
  sandbox-passed / 499 real-passed.
- **Correction:** the real `pytest` run on the dev machine (Python
  3.14.6, pytest 9.1.1) actually returned **1 failed, 568 passed** --
  not the "569 passed, 0 failed, 0 errors" this section originally (and
  wrongly) claimed before that run had actually been reported. The
  failure: `test_data_canonical.test_canonicalize_is_idempotent`
  compared two canonicalization passes with
  `pd.testing.assert_frame_equal()` (default `check_freq=True`); the
  two passes' `DatetimeIndex.freq` attributes differed (`<Day>` vs.
  `None`) even though values, columns, dtypes, and content hash were
  all identical -- `tz_localize()` (first pass, naive input) and
  `tz_convert()` (second pass, already-aware input) aren't guaranteed
  to preserve `.freq` identically across pandas versions (the sandbox's
  pandas 2.3.3 doesn't reproduce it; the dev machine's does).
- First fix attempt relaxed the test itself (`check_freq=False`). That
  was reverted as insufficient: `.freq` is pandas bookkeeping, not
  candle content, so letting two "canonical" outputs disagree on it --
  even just inside a test comparison -- undercuts the guarantee this
  module exists to provide. Real fix, in `src/data/canonical.py`:
  `canonicalize_candles()` now explicitly pins `DatetimeIndex.freq` to
  `None` on its output, so repeated canonicalization can never
  reintroduce a freq disagreement regardless of pandas version. Three
  regression tests added (`tests/test_data_canonical.py`:
  `test_canonicalize_clears_inferred_datetimeindex_freq`,
  `test_hash_of_canonical_output_is_stable_under_a_second_canonicalization_pass`;
  `tests/test_market_data.py`:
  `test_incremental_fetch_produces_the_same_hash_as_a_complete_fetch`,
  closing a real gap -- the existing incremental-fetch tests checked
  call counts and date ranges, not that the assembled result hashes
  identically to a one-shot fetch of the same range).
- Sandbox-reverified after the fix: **570 passed** (567 + 3 new
  regression tests), same 2 known environment-only failures, no other
  breakage.
- Confirmed via real `pytest` on the dev machine: **572 passed, 0
  failed, 0 errors, all green** (570 sandbox + both previously
  sandbox-only artifacts passing for real). Sprint 8 is now genuinely
  verified, not just sandbox-verified.

## Sprint 7 -- 2026-09-15, Portfolio-Aware Risk & Position Management

Moves the platform from simple per-trade allocation toward a
portfolio-aware risk and position-management system: genuine,
stop-based position sizing plus portfolio-level exposure constraints,
connected to the existing execution layer without redesigning it. See
`DECISIONS.md`, ADR-0039 for the full design and reasoning.

### Added

- `src/portfolio/position.py` (new) -- `PositionSide` (`LONG`/`SHORT`),
  `PositionLifecycle` (`OPEN`/`CLOSED` only -- see the module docstring
  for why there is no third `FLAT` member), `Position` (symbol, side,
  signed quantity, entry price/timestamp, optional stop/current price,
  lifecycle, realized/unrealized P&L). Broker-independent -- distinct
  from, and does not replace, `src.execution.models.Position`.
- `src/portfolio/models.py` -- new `Portfolio` class: cash plus open
  `Position`s, `equity`/`total_exposure`/`symbol_exposure()`/
  `position_count` computed properties, `open_position()`/
  `close_position()` (signed cash accounting, one open position per
  symbol), `to_account_state()` bridge to the existing `AccountState`.
  `AccountState` itself is untouched.
- `src/risk/models.py` -- `PortfolioRiskLimits` (`risk_pct_per_trade`,
  `max_symbol_exposure_pct`, `max_concurrent_positions`, `min_quantity`
  -- a class separate from `RiskLimits`, not new fields on it),
  `RejectionReason(str, Enum)` (13 members), `RiskDecision` (frozen
  dataclass carrying every intermediate quantity plus
  `as_sizing_decision()` to adapt into the existing `SizingDecision`
  shape). `RiskLimits`/`SizingDecision` themselves are untouched.
- `src/risk/portfolio_risk.py` (new) -- `PortfolioRiskEngine.decide(signal,
  portfolio, entry_price, stop_price) -> RiskDecision`: Stage A computes
  a stop-based `risk_quantity` (`floor(equity * risk_pct /
  abs(entry_price - stop_price))`); Stage B computes capital/allocation/
  total-exposure/symbol-exposure ceilings independently and takes the
  `min()` against `risk_quantity`, which is a hard ceiling the decision
  can never exceed. Rejects a same-symbol call with
  `POSITION_SCALING_NOT_SUPPORTED` (no add/reduce mechanism exists);
  raises on a `FLAT` signal (closing is never risk-gated).
- `src/execution/portfolio_sync.py` (new) -- `apply_fill_to_portfolio(portfolio,
  fill, stop_price=None) -> Position`: reads a `Fill` `PaperBroker`
  already produced and opens or closes the matching `Portfolio`
  position. The glue that keeps `PaperBroker`'s own bookkeeping and the
  new `Portfolio` in sync without either wrapping or modifying the
  other.
- `tests/test_portfolio_position.py` (27 tests) -- `Position`
  validation/computed properties/`close()`; `Portfolio` construction,
  long/short open/close cash math, multi-position support, defensive
  copies, `to_account_state()`.
- `tests/test_portfolio_risk.py` (36 tests) -- `PortfolioRiskLimits`
  validation, the spec's own worked sizing example, invalid-stop and
  zero-stop-distance cases, floor-not-round rounding, every individual
  constraint, the position-scaling/concurrent-position distinction,
  `RiskDecision` field contents (approved and rejected), all six
  mandatory worked examples, and the architectural invariant
  (`final_approved_quantity <= risk_quantity`) across five scenarios.
- `tests/test_sprint7_integration.py` (6 tests) -- the full Signal ->
  `PortfolioRiskEngine` -> `PaperBroker` -> `Fill` ->
  `apply_fill_to_portfolio` -> `Portfolio` pipeline for an approved
  trade and a full closure; a rejected decision that never reaches
  execution; the pre-existing symbol-mismatch invariant holding with a
  `RiskDecision` in the loop; three concurrent positions opened end to
  end; and existing exposure correctly constraining a new trade.
- `tests/test_architecture.py::test_risk_modules_do_not_import_src_broker_or_src_execution`
  -- confirms `src/risk` stays dependent only on `src/portfolio`/
  `src/signals`, never reaching into `src/broker`/`src/execution`
  itself.

### Changed

- None of `RiskLimits`, `SizingDecision`, `PositionSizer`,
  `AccountState`, `PaperBroker`, `src.execution.models.Position`,
  `Order`, or `Fill` changed -- every Sprint 7 addition is a new class,
  file, or optional-defaulted field.
- `src/portfolio/__init__.py`, `src/risk/__init__.py`,
  `src/execution/__init__.py` -- updated exports for the new classes/
  function.

### Decided

- A standalone, parallel risk-sizing path (`PortfolioRiskEngine`) rather
  than extending `PositionSizer` -- the two answer different questions
  (allocation vs. stop-based loss) and neither trade sized by one is
  meant to also be sized by the other.
- Two coexisting `Position` models, deliberately: the new
  `src.portfolio.position.Position` (broker-independent, risk-facing)
  and the existing `src.execution.models.Position` (fill-bookkeeping,
  `PaperBroker`-owned), kept in sync by
  `apply_fill_to_portfolio()` rather than merged or one wrapping the
  other.
- `Portfolio` duplicates state `PaperBroker` already tracks, by design
  -- avoids rewriting `PaperBroker`'s tested internals in exchange for a
  small, explicit synchronization step a caller performs once per fill.
- Position scaling stays unsupported and closing stays ungated by the
  risk engine, both matching `PaperBroker`'s existing one-open-position-
  per-symbol limitation (ADR-0022) rather than introducing new
  capability the execution layer can't actually honor.
- See `DECISIONS.md`, ADR-0039 for the full precedence-order rejection
  logic, the short-sale no-margin assumption, and everything explicitly
  deferred (Kelly, VaR/CVaR, correlation, optimization, real
  mark-to-market, etc.).

### Verified

- Confirmed via sandbox test runner (stub-based, `PYTHONPATH=/tmp/stubs:.
  python3 /tmp/runner_all.py`): **470 passed**, 2 known
  environment-only failures (`test_python_version_passes_against_running_interpreter`,
  `test_logger_is_importable_and_callable` -- both sandbox-stub
  artifacts, not real regressions; see `PROJECT_STATE.md`). Up from the
  Pre-Sprint 7 baseline of 402.
- Confirmed via real `pytest` on the dev machine (Python 3.14.6,
  pytest 9.1.1): **499 passed in 1.18s**, all green -- including both
  tests that failed only in the sandbox, confirming those were
  genuinely environment-only (sandbox Python 3.10 vs. the project's pin;
  the sandbox's own no-op `loguru` stub), not real regressions. This
  count is after the ADR-0040 cleanup pass below (497 sandbox-passed +
  2 known-environment-failures = 499 total collected, all passing for
  real).

## Sprint 7 cleanup -- 2026-09-16, Explicit Close-Path and Short-Margin Semantics, Formalized Risk/Execution Boundary

A targeted cleanup pass on Sprint 7 (`DECISIONS.md`, ADR-0040), not a
redesign: two review items (the `FLAT`/close path, and short-sale
capital/margin semantics) made explicit and typed, plus the
Risk/Execution/Portfolio responsibility boundary formalized and one
edge of it locked by a new architecture test. No existing behavior
changed -- `decide()` still raises on `FLAT`; `RiskLimits`,
`SizingDecision`, `PositionSizer`, `AccountState`, `PaperBroker`,
`Portfolio`, `Position`, and `apply_fill_to_portfolio()` are all
untouched.

### Added

- `src/risk/portfolio_risk.py`: `PortfolioRiskEngine.decide_close(signal,
  portfolio, quantity=None) -> RiskDecision` -- the symmetric
  close/exit-intent counterpart to `decide()`. A lookup-and-permit
  operation, never risk-sized: no risk budget computed, and
  `RiskLimits.max_portfolio_exposure_pct`/`PortfolioRiskLimits.
  max_symbol_exposure_pct` are never consulted, so a close remains
  permitted even when the portfolio is already over either limit. No
  open position -> `RejectionReason.NO_POSITION_TO_CLOSE` (new enum
  member); a `quantity` other than the position's own full size ->
  `RejectionReason.UNSUPPORTED_POSITION_OPERATION` (no partial-close
  support fabricated). Never submits an order itself -- the actual
  close still goes through `PaperBroker.submit_signal()` directly,
  unchanged.
- `src/risk/models.py`: `RejectionReason.NO_POSITION_TO_CLOSE`;
  `CapitalConstraintModel(str, Enum)` (`MODELED`/`NOT_MODELED`) makes a
  `SHORT`'s unmodeled capital ceiling machine-visible rather than an
  ambiguous `None`; `RiskDecision` gained `is_close: bool = False`,
  `signal_id: UUID | None = None`, `capital_model:
  CapitalConstraintModel | None = None` (all defaulted, no existing
  construction broken); `RiskDecision.to_trade_intent(quantity=None) ->
  ApprovedTradeIntent` formalizes the Risk -> Execution handoff and
  raises if the requested quantity exceeds `final_approved_quantity` --
  the `execution_quantity <= risk_approved_quantity` invariant enforced
  in code; `ApprovedTradeIntent` (new frozen dataclass) composes rather
  than duplicates `RiskDecision` (referenced, not copied).
- `tests/test_portfolio_risk.py` (+22 tests): close-path Cases A-D and
  the required-tests list, short-margin representation and its effect
  on `SHORT` trades (exposure/allocation/concurrent-position limits
  still bind; the absent capital ceiling is never read as unlimited),
  `to_trade_intent()`'s invariant enforcement (over-quantity rejection,
  execution-side reduction allowed, rejected-decision rejection).
- `tests/test_sprint7_integration.py` (+4 tests): the close path end to
  end via `decide_close()` + `PaperBroker` + `apply_fill_to_portfolio()`;
  close permitted over an already-breached exposure limit; a
  no-position close never reaching execution; "close is execution-owned"
  proving `PaperBroker.submit_signal()` needs no `sizing_decision` for a
  `FLAT` signal.
- `tests/test_architecture.py` (+1 test):
  `test_execution_does_not_duplicate_risk_sizing_logic` -- confirms
  `src/execution` never imports the risk-computation symbols
  (`PortfolioRiskEngine`, `PortfolioRiskLimits`, `RiskLimits`, or
  anything from `src.risk.portfolio_risk`), only the plain
  `SizingDecision` shape it has always depended on.

### Decided

- A new, separate `decide_close()` method rather than teaching `decide()`
  to accept `FLAT` -- a close is a fundamentally different question
  (permission, not sizing) and conflating the two methods would have
  been exactly the ambiguity this cleanup exists to remove. See
  `DECISIONS.md`, ADR-0040.
- `CapitalConstraintModel` as a paired, explicit field alongside
  `capital_quantity` rather than overloading `None`'s meaning or
  inventing a numeric sentinel.
- No `ExecutionResult` wrapper type: `Fill` (execution) and
  `Portfolio`/`Position` (state) already are the distinct, structured
  outputs those layers expose; a new wrapper with no consumer this
  round would itself be a speculative abstraction. No partial-close
  support -- fabricating it was explicitly out of scope.

### Verified

- Confirmed via sandbox test runner: **497 passed**, 2 known
  environment-only failures (unchanged from Sprint 7's own baseline
  set of known artifacts). Up from 470 before this cleanup.
- Confirmed via real `pytest` on the dev machine (Python 3.14.6,
  pytest 9.1.1): **499 passed in 1.18s**, all green.

## Pre-Sprint 7 -- 2026-09-15, Timeframe-Agnostic Architecture Corrections

A targeted correction pass, not a redesign, requested ahead of Sprint 7:
confirm the research/strategy architecture built in Sprints 2-6
genuinely supports daily, intraday, and minute-scale trading, and fix
only the assumptions that actually prevented it. Not an HFT platform,
and not building one this round -- no tick feeds, order-book
simulation, or microstructure modeling. See `DECISIONS.md`, ADR-0038
for the full architecture review and reasoning.

### Added

- `src/backtesting/metrics.py`: `infer_periods_per_year(index)` --
  estimates a Sharpe annualization factor from a `DatetimeIndex`'s own
  median timestamp spacing (252 for daily bars, scaled up accordingly
  for intraday ones) instead of a hardcoded, timeframe-blind constant.
- `Backtester.__init__(..., periods_per_year=None)` -- optional explicit
  override; defaults to inferring from `candles` at `run()` time.
- `scripts/run_experiment.py`: `--interval` CLI flag (choices
  constrained to `Interval`'s own values), threaded through to both the
  actual candle fetch and the recorded `ExperimentSpec` so the two can
  never silently disagree.
- `tests/test_timeframe_agnostic.py` (6 tests): the same
  `EMACrossStrategy` code and pipeline wiring run against daily and
  1-minute fixtures unmodified; two signals six minutes apart within
  one trading session both survive as a single precisely-timed trade;
  a 5.5-minute intraday hold attributes correctly (not zero, not
  date-truncated); `infer_periods_per_year()` returns 252 for daily
  spacing and two orders of magnitude higher for 1-minute spacing;
  `ExperimentSpec.interval` is confirmed a typed `Interval` that
  round-trips through the registry and rejects an unrecognized string.
- `tests/test_intraday_acceptance.py` (9 tests): direct, literal
  evidence against this correction's formal Acceptance Criteria (see
  the sprint completion report), one test per criterion that needed a
  dedicated check rather than being already covered elsewhere --
  intraday timestamps to the second surviving data through a trade
  unchanged (using the AC's own `09:42:13` example), two same-day
  signals both retained with distinct ids (`09:42:13`/`09:47:51`), no
  lookahead at 1-minute granularity, two experiments from different
  strategy classes carrying different fingerprints once persisted and
  reloaded from the registry, two experiments with an identical
  symbol/timeframe/range/source descriptor but revised candle content
  coexisting under different dataset hashes, a `typing.get_type_hints()`
  check that `Signal`/`Trade`/`ExperimentSpec`'s timestamp fields are
  `pd.Timestamp` (not `datetime.date`) and that signal generation has
  no artificial one-per-day cap, a full 1-minute strategy-to-attribution
  run, and a check that `ARCHITECTURE.md` states the minute-level-is-
  current / HFT-is-future-work capability boundary in those words.

### Changed

- `ExperimentSpec.interval` is now a typed `Interval`
  (`src/data/base.py`, the platform's pre-existing timeframe enum), not
  a bare `str` -- `__post_init__` normalizes a plain string input (e.g.
  `"1m"`) automatically, raising `ValueError` on an unrecognized value.
  `capture()`'s `interval` parameter accepts `Interval | str` for the
  same convenience. `ExperimentRegistry.save_spec()`/`get_spec()` store
  and restore the typed value intact.
- `calculate_metrics()`/`sharpe_ratio()` (`src/backtesting/metrics.py`):
  `periods_per_year` default changed from a hardcoded `252` to `None`
  (infer from the data); an explicit int is still accepted and always
  wins. Daily-bar behavior is byte-for-byte unchanged (median 1-day
  spacing infers to exactly 252); intraday behavior is corrected from
  silently, drastically wrong to order-of-magnitude-correct.
- `scripts/run_experiment.py`'s `main()` summary print no longer calls
  `spec.dataset_start.date()` (which discarded time-of-day) -- prints
  the full timestamp instead.

### Verified

- Reviewed and found already timeframe-agnostic, no change needed:
  `Signal.timestamp`/`Trade.entry_time`/`exit_time`/
  `ExperimentSpec.dataset_start`/`dataset_end` (all full-precision
  `pd.Timestamp`); the `Strategy`/`BaseStrategy` contracts (no
  one-signal-per-day assumption anywhere); `Backtester`'s trade
  extraction, position series, and equity curve (already row-based, not
  calendar-day-based); `CacheManager`'s CSV round-trip (empirically
  confirmed: 1-minute `DatetimeIndex` survives with full precision);
  `YFinanceProvider._normalize()` (strips timezone, never truncates
  time-of-day). ADR-0006 (timezone consistency, still deferred) is
  unaffected -- no second, competing timezone system was introduced.
- Confirmed via real `pytest` on the dev machine (Python 3.14.6,
  pytest 9.1.1): **402 passed in 1.12s**, all green -- up from the
  Sprint 6 baseline of 386 (392 after the initial architecture
  corrections, 402 after adding `tests/test_intraday_acceptance.py`'s
  formal Acceptance Criteria evidence). The
  `test_python_version_passes_against_running_interpreter` failure seen
  in the sandbox run was confirmed environment-only, as expected -- it
  passes on the dev machine's pinned interpreter.
- Formal Acceptance Criteria (AC-01 through AC-19) evidence: see the
  sprint completion report delivered alongside this entry, citing the
  exact test/file/assertion satisfying each criterion.

## Sprint 6 (complete) -- 2026-09-15, Research Pipeline & Experiment Integrity (part 2, close-out)

Closes both items left open at the end of part 1 (`ROADMAP.md`'s
"What's left before Sprint 6 can close"): a second registered strategy
proving the registry/`ExperimentSpec` seam genuinely generalizes, and a
worked example wiring one real experiment through the full pipeline.
Sprint 6 is now complete.

### Added

- `src/strategies/rsi_mean_reversion.py` -- `RSIMeanReversionStrategy`,
  registered as `"rsi_mean_reversion"` (`DECISIONS.md`, ADR-0037). The
  platform's second permanent strategy, and deliberately the *opposite*
  trading idea from `EMACrossStrategy` (mean reversion, not trend
  following) rather than a parameter variant of it. Long-only: enters
  `LONG` the first time RSI drops to or below `oversold` (default
  `30.0`), exits to `FLAT` the first time RSI, while in that position,
  rises to or above `overbought` (default `70.0`). Exposes a `params`
  property (`period`, `oversold`, `overbought`, `confidence`) for
  `ExperimentSpec.capture()`, same as `EMACrossStrategy`.
- `scripts/run_experiment.py` + `scripts/__init__.py` -- a worked
  example wiring one experiment through Strategy -> Backtest -> Risk ->
  Execution -> Attribution -> Research Report -> Experiment Registry.
  `run_experiment(strategy_name, symbol, candles, ...)` is a plain,
  network-free core function; `main()` is a thin argparse CLI wrapper
  that is the only code path touching the network
  (`MarketDataService().get_history(...)`). Run as:
  `python scripts/run_experiment.py --symbol SPY --strategy ema_cross`.
  Deliberately placed outside `src/` -- see ADR-0037.
- `tests/test_rsi_mean_reversion_strategy.py` (14 tests) -- mirrors
  `tests/test_ema_cross_strategy.py`'s structure and rigor: constructor
  validation, `.name`, `prepare()` matching `IndicatorEngine` output,
  no-signal and warmup cases, a full oversold-to-overbought cycle with a
  sparsity assertion, a never-emits-SHORT check, confidence/metadata
  checks, and an end-to-end `Backtester().run()` smoke test.
- `tests/test_architecture.py::test_second_strategy_required_no_changes_to_core_pipeline_modules`
  -- greps `src/backtesting`, `src/experiments/registry.py`,
  `src/attribution`, `src/research`, and `src/broker` for any mention of
  the new strategy and fails if it finds one. Direct, structural proof
  of Sprint 6's closing architectural principle, not just a docstring's
  claim.
- `tests/test_run_experiment_script.py` (7 tests) -- exercises
  `run_experiment()` against synthetic, network-free candles for *both*
  `"ema_cross"` and `"rsi_mean_reversion"`, plus `_parse_args()`.

### Decided

- The second strategy is a genuinely different trading idea (mean
  reversion), not a variant of the first -- a relabeled EMA-cross
  wouldn't have tested anything the registry seam didn't already prove.
  See `DECISIONS.md`, ADR-0037.
- The worked example lives in `scripts/`, not `src/` -- avoids
  committing to a permanent orchestration API (`PaperTradingLoop`,
  ADR-0021) this round. Its core logic is a plain function, separate
  from its network-touching CLI wrapper, specifically so it stays
  directly testable without hitting the network.

### Verified

- Confirmed via real `pytest` on the dev machine (Python 3.14.6):
  **386 passed in 0.92s** -- all tests green, including
  `test_python_version_passes_against_running_interpreter` -- up from
  the Sprint 6 part 1 baseline of 364.

## Sprint 6, part 1 -- 2026-09-15, Research Pipeline & Experiment Integrity

The first step toward the platform's long-term reproducibility target
(`ROADMAP.md`): experiment -> strategy/version -> parameters ->
dataset/version -> signals -> trades -> metrics -> attribution ->
report. Not the entire chain -- the reproducible starting point of it,
plus proof (one deliberately thorough end-to-end test, not thirty
superficial ones) that every existing module actually composes.

### Added

- `src/experiments/spec.py` -- `ExperimentSpec` (`DECISIONS.md`,
  ADR-0035): an immutable record of `strategy_name`,
  `strategy_version`, `strategy_params`, `symbol`, `interval`,
  `dataset_start`/`dataset_end`, `dataset_source`,
  `dataset_fingerprint`, `risk_config`, and a reserved
  `backtest_config` placeholder. `ExperimentSpec.capture(strategy,
  candles, risk_limits, symbol=..., interval=..., dataset_source=...)`
  builds one from a strategy instance and the candles it ran against.
  `reconstruct_strategy()` rebuilds an equivalent strategy instance
  purely from the stored name + params; `verify_strategy_version()` and
  `verify_dataset(candles)` each return `False` when the recorded
  identity no longer matches current reality.
- `src/strategies/identity.py` -- `strategy_version(cls)`: SHA-256 of a
  strategy class's own Python source. Automatic, not something an
  author has to remember to bump -- change `EMACrossStrategy`'s logic
  six months from now, and its version changes with it.
- `src/strategies/registry.py` -- `@register_strategy("name")` /
  `get_strategy_class(name)` / `available_strategies()`, the same
  `@register_x` pattern `src/indicators/registry.py` and
  `src/cli/registry.py` already use. `EMACrossStrategy` now registers
  itself as `"ema_cross"` -- this is what makes
  `ExperimentSpec.reconstruct_strategy()` possible.
- `src/strategies/sdk.py` -- `BaseStrategy.params` (new optional
  property, defaults to `{}`); `EMACrossStrategy.params` overrides it
  to return `{"fast", "slow", "confidence"}`, read by
  `ExperimentSpec.capture()`.
- `src/utils/hashing.py` -- `sha256_hex()` and `dataframe_fingerprint()`
  (hashes a DataFrame's actual values + index + columns via
  `pandas.util.hash_pandas_object`) -- the shared basis for both
  identity seams above.
- `ExperimentRegistry.save_spec(experiment_id, spec)` /
  `get_spec(experiment_id)` -- a new `experiment_specs` table, one row
  per experiment, additive and separate from `log_experiment()` (same
  reasoning as `save_signals()`, ADR-0016).
- `PaperBroker.submit_signal()` now raises `ValueError` if its `symbol`
  argument disagrees with `signal.symbol` (`DECISIONS.md`, ADR-0036) --
  closes the gap flagged as deferred in ADR-0033/`PROJECT_STATE.md`'s
  Technical Debt. Checked first, before any other validation or side
  effect.
- `tests/test_hashing.py` (6 tests), `tests/test_strategy_registry.py`
  (9 tests), `tests/test_experiment_spec.py` (12 tests) -- direct
  coverage of the new modules.
- `tests/test_pipeline_contract.py` (3 tests) -- the single end-to-end
  contract test: Strategy -> Signal -> Backtest -> Risk -> Execution ->
  Trade -> Performance -> Attribution -> Experiment Registry -> spec ->
  reconstruction, using the same known-good EMA-crossover candle
  fixture already proven elsewhere in the suite. Asserts a strategy
  rebuilt purely from its stored `strategy_name`/`strategy_params`
  reproduces the identical sequence of decisions (timestamp, symbol,
  direction, confidence) as the original run, and that both
  `verify_strategy_version()` and `verify_dataset()` correctly detect
  drift when the underlying code or data changes.
- `tests/test_execution.py` -- new symbol-mismatch rejection test
  (ADR-0036).

### Decided

- Strategy version = a hash of the strategy's own source, not Git
  commit hashing (ties identity to repository state -- explicitly out
  of scope) and not a manually maintained `VERSION` string (relies on
  an author remembering to bump it -- the exact failure mode this ADR
  exists to close). A source-identity hash, not a semantic-identity
  one -- a purely cosmetic edit also changes the version. Accepted
  trade-off, not a bug. See `DECISIONS.md`, ADR-0035.
- Dataset identity = a content hash of the actual candles used, not a
  plain `(symbol, interval, start, end, source)` descriptor (can't
  detect silently-changed underlying values) and not a full dataset
  versioning/snapshot system (explicitly out of scope this round). See
  ADR-0035.
- `BaseStrategy.params` is optional and defaults to `{}` -- not
  introspected automatically from `__init__`, and not added to the
  `Strategy` Protocol itself. Avoids forcing an interface change onto
  every existing and future strategy.
- `PaperBroker.submit_signal()`'s separate `symbol` parameter is kept,
  not removed or folded into `signal.symbol` -- validated for
  agreement instead. See ADR-0036.
- Not built this round (the seam, not the system): dataset
  snapshotting/archival, strategy source-code archival, Git/package
  version integration, automatic re-run scheduling, `Trade.symbol`,
  wiring `PositionSizer`/`Backtester` together, and the complete
  experiment artifact graph (trades/attribution/reports linked into the
  registry). All tracked as future work, not silently dropped.

### Verified

- Confirmed via real `pytest` on the dev machine (Python 3.14.6):
  **364 passed in 0.87s** -- all tests green, including
  `test_python_version_passes_against_running_interpreter` -- up from
  the Pre-Sprint 6 baseline of 329.

## Pre-Sprint 6 -- 2026-09-15, Architecture Review Cleanup

A targeted correction pass ahead of Sprint 6, not a redesign -- four
architectural inconsistencies an architecture review surfaced in
Sprint 4-5's work, fixed before new feature work starts. Every existing
ADR's deliberate deferral (confidence-scaled sizing, stop-based risk,
real mark-to-market, Tiger/IB order lifecycles, IG's structural
`get_order`/`cancel_order` gap) was left exactly as deferred.

### Added / Changed

- `src/portfolio/` (new) -- `AccountState` moved here from `src/risk`,
  unchanged in fields/validation/semantics. `src/broker/base.py` and all
  four concrete brokers, `src/risk/engine.py`, `src/risk/__init__.py`,
  and `src/execution/engine.py` now import it from
  `src.portfolio.models` instead of `src.risk.models` -- `src/broker`
  no longer depends on `src/risk` to describe an account. See
  `DECISIONS.md`, ADR-0031.
- `src/risk/models.py` -- `RiskLimits.risk_per_trade_pct` renamed to
  `allocation_per_trade_pct` (and `PositionSizer`'s `reason` strings
  updated to match). Behavior completely unchanged -- still a fixed
  fraction of equity. `RiskLimits`'s docstring now states explicitly
  that this is capital allocation, not maximum loss, and that
  stop-based risk sizing is a distinct, unbuilt capability. See
  `DECISIONS.md`, ADR-0032.
- `src/signals/models.py` -- `Signal` gained a required, first-class
  `symbol: str` field (not a `metadata` key). `BaseStrategy`
  (`src/strategies/sdk.py`) now takes a required `symbol` constructor
  argument and attaches it to every `Signal` it emits;
  `EMACrossStrategy` threads it through. `ExperimentRegistry`
  (`src/experiments/registry.py`) gained a `symbol` column on the
  `signals` table (`save_signals()`/`_row_to_signal()` updated). The
  Backtester needed no change -- `Signal` objects pass through
  `BacktestResult.signals` untouched. See `DECISIONS.md`, ADR-0033.
- `src/research/models.py`, `src/research/reporter.py` --
  `ResearchReport` gained `renderer_error: str | None`.
  `ResearchReporter.run()` now records the actual exception message when
  a `ClaudeNarrativeRenderer` fails and falls back, leaving it `None` on
  the normal (unconfigured-Claude) fallback path. `rendered_by` and the
  deterministic `findings` are unaffected either way. See
  `DECISIONS.md`, ADR-0034.
- `src/execution/engine.py`, `src/broker/base.py`, `src/broker/__init__.py`
  -- docstrings strengthened: `PaperBroker.account_state.equity`'s
  not-marked-to-market limitation is now stated in multiple places, and
  `BrokerConnection`'s doc now names the three different reasons a
  concrete broker's method can raise `NotImplementedError` (not yet
  built vs. structurally impossible given the broker's own API vs.
  genuinely unsupported). No behavior changed.
- `tests/test_architecture.py` (new, 8 tests) -- protects the corrected
  contracts directly: broker modules don't import `src.risk` (static
  source check), `src/portfolio` depends on nothing else in this
  codebase, `AccountState` no longer defined in `src/risk/models.py`,
  `PositionSizer` consumes the neutral `AccountState`, `Signal(...)`
  without `symbol` raises `TypeError`, a full strategy -> backtest ->
  registry round trip preserves both `symbol` and signal UUID identity,
  and `PaperBroker` has no mark-to-market mechanism (a structural
  tripwire against silently reintroducing that capability without a
  deliberate ADR).
- `tests/test_portfolio.py` (new, 5 tests) -- `AccountState` validation,
  moved verbatim from `tests/test_risk.py`.
- `tests/test_risk.py`, `tests/test_signals.py`, `tests/test_backtesting.py`,
  `tests/test_experiments.py`, `tests/test_strategy_sdk.py`,
  `tests/test_ema_cross_strategy.py`, `tests/test_execution.py`,
  `tests/test_integration_paper_trading.py`, `tests/test_broker.py`,
  `tests/test_ibkr.py`, `tests/test_ig.py`, `tests/test_tiger.py`,
  `tests/test_cli_doctor.py`, `tests/test_research.py` -- updated for
  the `AccountState` import path, the `allocation_per_trade_pct` rename,
  and `Signal`'s new required `symbol` field (every constructor call
  site across the suite), plus new coverage for `renderer_error` (a
  successful Claude render, the normal no-key fallback, a renderer that
  raises, and deterministic findings surviving a renderer failure).

### Decided

- `AccountState` moves to a new, neutral `src/portfolio` package rather
  than staying in `src/risk` with a re-export, so the class itself (not
  just its import path) lives outside both `src/broker` and `src/risk`.
- Rename, don't invent a stop-loss model just to make
  `risk_per_trade_pct`'s name technically true -- the terminology was
  wrong, not the math.
- `symbol` is a required, non-defaulted field on `Signal`, migrated
  across every call site in one controlled pass, rather than optional
  or metadata-based -- a `Signal` should always be independently
  identifiable.
- `ClaudeNarrativeRenderer` failures are surfaced via a new
  `renderer_error` field, not a raised exception or a change to
  `rendered_by`'s existing two values.
- `requirements.txt`'s direct-vs-transitive dependency mixing was
  inspected and deliberately left alone this round -- documented as
  pre-v1.0 debt in `ROADMAP.md` rather than risking a disruptive
  dependency change in a narrowly-scoped cleanup.
- No new ADR for the `PaperBroker` mark-to-market documentation
  strengthening -- it's a documentation clarification of an existing
  decision (ADR-0022), not a new architectural decision.

### Verified

- Confirmed via real `pytest` on the dev machine (Python 3.14.6):
  **329 passed in 1.26s** -- all tests green, including
  `test_python_version_passes_against_running_interpreter`, which only
  fails in the sandbox's Python 3.10 environment (this project requires
  >=3.12).

## Sprint 5 (complete) -- 2026-09-10, Broker Connectivity

### Added

- `src/broker/` -- `BrokerConnection` + `AlpacaBroker` (`DECISIONS.md`,
  ADR-0023), touching 1 existing file (Extension Cost: 1:
  `src/cli/checks.py`):
  - `base.py` -- `BrokerConnection(ABC)`, analogous to `src/data`'s
    `DataProvider`: one abstract method, `get_account() ->
    src.risk.AccountState` -- reuses the existing account-state
    currency `PositionSizer` already consumes from `PaperBroker`,
    rather than inventing a parallel "BrokerAccount" model.
  - `alpaca.py` -- `AlpacaBroker(BrokerConnection)`. Reads
    `ALPACA_API_KEY`/`ALPACA_API_SECRET` from constructor arguments or
    environment variables (same convention as `ANTHROPIC_API_KEY`);
    raises `BrokerAuthenticationError` immediately if either is
    missing, before any network call. Defaults to Alpaca's **paper**
    endpoint (`ALPACA_PAPER_BASE_URL`) -- the live endpoint requires an
    explicit override, never a default. `get_account()` calls
    `GET /v2/account` via an injectable HTTP session (defaults to
    `requests`), maps 401/403 to `BrokerAuthenticationError`, any other
    non-200 or network failure to `BrokerConnectionError`, and parses a
    successful response into an `AccountState` (`open_exposure` = sum
    of absolute long + short market value).
  - `exceptions.py` -- `BrokerError`, `BrokerAuthenticationError`,
    `BrokerConnectionError`.
  - Order submission against a real broker is deliberately **not**
    included this round -- a real order's asynchronous lifecycle
    (pending/partial/rejected) doesn't fit `src/execution`'s
    synchronous, instant-fill `Order`/`Fill` model (ADR-0022); designing
    that properly is a separate, later step.
- `tests/test_broker.py` -- 14 tests, all against an injected fake HTTP
  session (no real network, no real Alpaca account, matching the
  existing "unit-test the interface, leave the live vendor to a future
  integration suite" posture toward `YFinanceProvider`): credential
  validation (missing, argument-supplied, environment-supplied, only
  one of the two present), paper-endpoint default and explicit live
  override, successful parsing (equity, long+short exposure summed,
  missing fields default to zero), credentials sent as headers, and
  error mapping (401/403 -> auth error, other HTTP failure -> connection
  error, network exception -> connection error).

### Changed

- `src/cli/checks.py` -- `check_broker_connection` upgraded from an
  unconditional `NOT_IMPLEMENTED` to a real check: still
  `NOT_IMPLEMENTED` when `ALPACA_API_KEY`/`ALPACA_API_SECRET` aren't
  set, otherwise a live `OK`/`FAIL` via `AlpacaBroker().get_account()`.
  Same shape of change as ADR-0020's `check_api_keys` upgrade.
- `tests/test_cli_doctor.py` -- replaced
  `test_broker_connection_is_not_implemented` with three tests: not
  implemented when credentials are absent, `OK` when a faked
  `AlpacaBroker` succeeds, `FAIL` when it raises.

### Decided

- Alpaca over Interactive Brokers for the first integration -- a REST
  API with no local software required and a built-in paper-trading
  environment, matching the platform's "deliberately simple first"
  posture. See `DECISIONS.md`, ADR-0023.
- Built and tested now against a fake HTTP session, without a real
  account -- real credentials can be added later via environment
  variables with zero code changes.
- Scoped to connectivity + account state only this round; order
  submission is deferred to its own round rather than forcing it into
  `src/execution`'s existing synchronous fill model where it doesn't
  actually fit.
- Defaults to Alpaca's paper-trading endpoint; the live, real-money
  endpoint requires an explicit override, never a default.

### Verified

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **213
  passed**, 0 failed.

### Added (Order Submission)

- `src/broker/models.py` (new) -- `OrderSide`, `OrderStatus`,
  `OrderRequest`, `BrokerOrder` (`DECISIONS.md`, ADR-0024). Deliberately
  independent of `src.execution.models` -- duplicates the two-value
  `OrderSide` enum rather than importing across a boundary that
  `ARCHITECTURE.md` draws running the other direction (`execution -->
  broker`, aspirational).
- `src/broker/base.py` -- `BrokerConnection` gains two abstract methods:
  `submit_order(request: OrderRequest) -> BrokerOrder` and
  `get_order(broker_order_id: str) -> BrokerOrder`.
- `src/broker/alpaca.py` -- `AlpacaBroker.submit_order()` posts to
  `POST /v2/orders` (market order, day time-in-force); `get_order()`
  reads `GET /v2/orders/{id}`. Both share a new `_request()` helper with
  `get_account()` (network exceptions, 401/403 -> auth error, other
  non-2xx -> connection error, all previously inlined only in
  `get_account()`). Alpaca's raw order status strings map to
  `OrderStatus` via a new `_STATUS_MAP`; an unrecognized status raises
  `BrokerConnectionError` rather than guessing. No cancellation this
  round.
- `src/broker/__init__.py` -- exports `BrokerOrder`, `OrderRequest`,
  `OrderSide`, `OrderStatus`.
- `tests/test_broker.py` -- extended from 14 to 29 tests: `OrderRequest`
  validation (rejects non-positive quantity), `submit_order` request
  body + parsed response, side mapping (buy/sell), `get_order` URL +
  fill-info parsing, status-mapping coverage across representative
  Alpaca statuses, unrecognized-status error, and submit/get error paths
  mirroring `get_account`'s (401/403 -> auth error, other HTTP failure
  -> connection error, network exception -> connection error).
- Extension Cost: 0 file(s) changed outside `src/broker/` (all changes
  contained within the package that already owns this capability).

### Decided (Order Submission)

- Kept `src/broker`'s order models independent of `src.execution`,
  rather than reusing `Order`/`OrderSide` from there, to avoid locking
  in a backwards dependency (broker depending on execution) the first
  time it was convenient. See `DECISIONS.md`, ADR-0024.
- Scope stops at submit + status check -- no order cancellation this
  round.
- An unrecognized Alpaca order status raises rather than silently
  defaulting, matching `atp doctor`'s "never fake a pass" posture.

### Verified (Order Submission)

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **228
  passed**, 0 failed.

### Added (Order Cancellation)

- `src/broker/base.py` -- `BrokerConnection.cancel_order(broker_order_id:
  str) -> None` (`DECISIONS.md`, ADR-0025), rounding out order
  management to submit + status + cancel.
- `src/broker/alpaca.py` -- `AlpacaBroker.cancel_order()` calls
  `DELETE /v2/orders/{id}`. Returns `None` -- confirms the broker
  *accepted* the cancellation, not that the order actually ended up
  canceled (cancellation is asynchronous; the order may already have
  filled). Callers wanting the actual outcome call `get_order()`
  afterward. `_Session` Protocol gained `.delete()`; the shared
  `_request()` helper gained a `parse_json=False` option to accept
  Alpaca's `204 No Content` response without trying to parse a body.
- `tests/test_broker.py` -- extended from 29 to 34 tests: `FakeSession`
  gained `.delete()`; new tests cover the expected URL/method, a
  `None` return on success, and error mapping (401/403 -> auth error,
  other HTTP failure (e.g. 422 for a non-cancelable order) -> connection
  error, network exception -> connection error).
- Extension Cost: 0 file(s) changed outside `src/broker/`.

### Decided (Order Cancellation)

- `cancel_order` returns `None`, not a `BrokerOrder` -- a status
  fetched immediately after cancellation would imply more certainty
  than a real, asynchronous cancellation can honestly provide. See
  `DECISIONS.md`, ADR-0025.

### Verified (Order Cancellation)

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **233
  passed**, 0 failed.

### Added (Second Broker -- Interactive Brokers)

- `src/broker/ibkr.py` (new) -- `IBKRBroker(BrokerConnection)`
  (`DECISIONS.md`, ADR-0026), the platform's second concrete broker --
  proof `BrokerConnection` is actually swappable. Built against IB's
  **Client Portal Web API** (REST-based, fits the existing injectable
  `_Session` pattern), not the socket-based TWS API. Takes no
  credential arguments -- IB's individual/retail auth is a browser
  session against a locally running Client Portal Gateway, not a simple
  key/secret pair; a call raises `BrokerAuthenticationError` if the
  gateway reports the session isn't authenticated. No separate
  paper/live endpoint (unlike Alpaca) -- that's determined by which
  account is logged into the gateway. `get_account()` resolves the
  account via `GET /iserver/accounts` (`selectedAccount`, falling back
  to the first listed account) then reads `netliquidation`/
  `grosspositionvalue` from `GET /portfolio/{accountId}/summary` into an
  `AccountState`. `submit_order`/`get_order`/`cancel_order` all raise
  `NotImplementedError` this round -- IB's order flow (conid lookup,
  reply/confirmation handling) is deferred to its own round.
- `src/broker/__init__.py` -- exports `IBKRBroker`,
  `IBKR_GATEWAY_BASE_URL`.
- `tests/test_ibkr.py` (new, 15 tests) -- against an injected fake HTTP
  session, no real gateway or account: interface conformance
  (`isinstance(broker, BrokerConnection)`), base URL default/override,
  no-credentials-required construction, `get_account` success
  (`selectedAccount` resolution, first-of-list fallback, missing
  `grosspositionvalue` defaults to zero), error paths (no accounts ->
  connection error, 401/403 -> auth error, other HTTP failure ->
  connection error, network exception -> connection error), and
  `submit_order`/`get_order`/`cancel_order` all raising
  `NotImplementedError`.
- Extension Cost: 1 file changed outside the new `ibkr.py`/
  `test_ibkr.py` -- `src/broker/__init__.py` (exports).

### Decided (Second Broker -- Interactive Brokers)

- Client Portal Web API over the TWS API -- REST-based, fits the
  existing `_Session` pattern without a new socket transport model.
- No credential validation in the constructor -- nothing to validate;
  IB's session lives in the Client Portal Gateway itself.
- `submit_order`/`get_order`/`cancel_order` raise `NotImplementedError`,
  not a `BrokerError` -- a known implementation gap, not a broker-side
  failure. See `DECISIONS.md`, ADR-0026.

### Verified (Second Broker -- Interactive Brokers)

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **248
  passed**, 0 failed.

### Added (Fill Reconciliation)

- `src/reconciliation/` (new package, `DECISIONS.md`, ADR-0027) --
  compares what `PaperBroker` assumes (instant, complete fill at a
  caller-supplied price) against what a real broker order actually did:
  - `models.py` -- `FillReconciliation`: side-normalized
    `price_slippage_per_share`/`price_slippage_pct` (positive always
    means real execution was worse than simulated, regardless of
    BUY/SELL), `quantity_shortfall` (for partial fills), `cost_impact`
    (dollar impact using the real filled quantity).
  - `engine.py` -- `reconcile_fill(real_order: BrokerOrder,
    simulated_fill: Fill) -> FillReconciliation`. Validates matching
    symbol and side (compared by `.value`, since `OrderSide` is two
    separate enums per ADR-0024) and that `real_order` has actually
    filled (`FILLED`/`PARTIALLY_FILLED`) before comparing.
  - Deliberately depends on both `src.execution` and `src.broker` --
    a documented, intentional exception to ADR-0024's independence
    rule, since this is a comparison/analysis layer neither of those
    two capabilities depends back on (same shape as `src/attribution`
    depending on `src/backtesting` + `src/regime`).
  - Single-order comparison primitive only this round -- aggregating
    across many trades into a summary report is a natural future step.
- `tests/test_reconciliation.py` (new, 11 tests) -- exact match (zero
  slippage), price slippage sign normalization for both BUY and SELL,
  partial-fill quantity shortfall, cost impact using real filled
  quantity, and validation (symbol mismatch, side mismatch, unfilled/
  rejected/canceled order, missing `filled_avg_price`).
- Extension Cost: 0 file(s) changed outside the new
  `src/reconciliation/` package.

### Decided (Fill Reconciliation)

- `src/reconciliation` may depend on both `src/execution` and
  `src/broker` -- a deliberate, documented exception to ADR-0024's
  broker/execution independence rule, since this is a higher-level
  analysis layer, not a capability either one depends on. See
  `DECISIONS.md`, ADR-0027.
- Single-order primitive only this round, not a batch/aggregate report.
- All slippage/cost fields are side-normalized so "positive" always
  means "worse than simulated," regardless of order direction.

### Verified (Fill Reconciliation)

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **282
  passed**, 0 failed (verified together with the IG addition below).

### Added (Third Broker -- IG)

- `src/broker/ig.py` (new) -- `IGBroker(BrokerConnection)`
  (`DECISIONS.md`, ADR-0028), the platform's third concrete broker --
  and the first second-broker candidate the platform's actual user can
  use with a real account, since IG doesn't geo-restrict access the way
  Interactive Brokers does. Built against IG's plain REST Trading API
  (no local gateway process required, unlike IB). Credentials are a
  third distinct shape in this codebase: an API key *plus* username and
  password (`IG_API_KEY`/`IG_USERNAME`/`IG_PASSWORD`, env-var fallback),
  exchanged once via `POST /session` for short-lived `CST`/
  `X-SECURITY-TOKEN` session tokens, cached for the instance's lifetime
  (no refresh logic this round). `IG_DEMO_BASE_URL`/`IG_LIVE_BASE_URL`
  select environment explicitly, like Alpaca's pair (unlike IB).
  `get_account()` resolves the account (explicit `account_id`, IG's own
  "preferred" account, or the first listed) and maps `balance.balance`
  -> equity, `balance.deposit` (margin committed) -> open exposure --
  a documented approximation, since CFD/spread-bet accounts are
  margined rather than fully paid. `submit_order`/`get_order`/
  `cancel_order` all raise `NotImplementedError` -- IG's order model
  (a short-lived deal reference confirmed into a permanent position, no
  ongoing order-status endpoint) doesn't fit `BrokerOrder`/
  `OrderStatus` without its own design round.
- `src/broker/__init__.py` -- exports `IGBroker`, `IG_DEMO_BASE_URL`,
  `IG_LIVE_BASE_URL`.
- `tests/test_ig.py` (new, 23 tests) -- against an injected fake HTTP
  session, no real IG account: interface conformance, credential
  validation, demo/live default/override, login flow (token extraction
  from response headers, caching across calls, only one login per
  instance), account resolution (preferred/explicit/first-listed),
  missing-deposit default, error paths (no accounts, unknown
  `account_id`, 401/403 on login or mid-use, other HTTP failure,
  network exception, missing tokens in login response), and
  `submit_order`/`get_order`/`cancel_order` all raising
  `NotImplementedError`.
- Extension Cost: 1 file changed outside the new `ig.py`/`test_ig.py`
  (`src/broker/__init__.py`, exports).

### Decided (Third Broker -- IG)

- IG over Tiger Trade for this round -- both looked viable, IG was
  picked to build first.
- Session-based login-for-tokens auth, cached per instance, no refresh
  logic yet.
- Connectivity + account state only -- IG's deal-reference/confirm
  order model doesn't fit `BrokerOrder`/`OrderStatus` without its own
  design round. See `DECISIONS.md`, ADR-0028.

### Verified (Third Broker -- IG)

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **282
  passed**, 0 failed.

### Added (IG Order Submission)

- `src/broker/ig.py` -- `IGBroker.submit_order()` implemented
  (`DECISIONS.md`, ADR-0029): `POST /positions/otc` then
  `GET /confirms/{dealReference}`, resolving **synchronously** into a
  final `BrokerOrder` (`FILLED` on `dealStatus == "ACCEPTED"`,
  `REJECTED` on `"REJECTED"`; an unrecognized status raises
  `BrokerConnectionError`). `currencyCode` for the order body is read
  from the selected account's own `currency` field -- never guessed or
  hardcoded -- via a new shared `_get_selected_account()` helper reused
  by both `get_account()` and `submit_order()`. `get_order()`/
  `cancel_order()` stay `NotImplementedError` with an updated message:
  there's no live status endpoint to poll and nothing left to cancel
  once a market order has resolved.
- `tests/test_ig.py` -- extended: order body/currency resolution, FILLED
  on ACCEPTED, REJECTED mapping, side mapping, unrecognized-status
  error, and error paths (no accounts for currency resolution, 401/403,
  other HTTP failure, network exception).
- Extension Cost: 0 file(s) changed outside `ig.py`/`test_ig.py`.

### Decided (IG Order Submission)

- `get_order()`/`cancel_order()` stay `NotImplementedError` rather than
  approximating status via a position-existence lookup, which couldn't
  reliably distinguish "closed after filling" from "never existed."
- Order currency is read from the account itself, not guessed or added
  as a new constructor parameter. See `DECISIONS.md`, ADR-0029.

### Verified (IG Order Submission)

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **290
  passed**, 0 failed.

### Added (Fourth Broker -- Tiger Trade)

- `src/broker/tiger.py` (new) -- `TigerBroker(BrokerConnection)`
  (`DECISIONS.md`, ADR-0030), the platform's fourth concrete broker, and
  the first built by wrapping an official vendor SDK (`tigeropen`)
  instead of talking `requests` directly -- Tiger's auth requires
  RSA-signing every request (PKCS#1 private key), too risky to
  hand-roll against raw HTTP. The injectable seam is the SDK's
  `TradeClient` object (`_TradeClient` Protocol: `get_assets(segment,
  market_value) -> list`), not an HTTP session. `tigeropen` is imported
  lazily, only inside `_build_client()`, and is deliberately **not**
  added to `requirements.txt`, mirroring ADR-0020's `anthropic`
  precedent. Credentials (`tiger_id`/`private_key_path`/`account`,
  env-var fallback `TIGER_ID`/`TIGER_PRIVATE_KEY_PATH`/`TIGER_ACCOUNT`)
  are a fourth distinct auth shape in this codebase. No separate paper/
  live URL -- like `IBKRBroker`, the `account` value itself decides.
  `get_account()` calls `get_assets(segment=False, market_value=True)`,
  wraps any exception into `BrokerConnectionError` (a documented,
  provisional limitation), and maps `summary.net_liquidation` ->
  equity, `summary.gross_position_value` (defaulting to `0.0`) -> open
  exposure -- the most direct account mapping of any broker so far.
  `submit_order`/`get_order`/`cancel_order` all raise
  `NotImplementedError` -- connectivity and account state only this
  round, even though Tiger's API looks like it supports a real order
  lifecycle more cleanly than IG's does.
- `src/broker/__init__.py` -- exports `TigerBroker`; docstring updated.
- `tests/test_tiger.py` (new, 15 tests) -- against an injected fake
  `_TradeClient`, never `tigeropen` itself: interface conformance,
  credential validation (missing tiger_id/private_key_path/account,
  env-var fallback, argument precedence over environment), default
  `sandbox_debug`, `get_account()` success (net_liquidation/
  gross_position_value parsing, missing gross_position_value defaults
  to zero, first-portfolio-used-when-multiple), no-portfolios error,
  arbitrary client exception wrapped into `BrokerConnectionError`, and
  `submit_order`/`get_order`/`cancel_order` all raising
  `NotImplementedError`.
- Extension Cost: 1 file changed outside the new `tiger.py`/
  `test_tiger.py` (`src/broker/__init__.py`, exports + docstring).

### Decided (Fourth Broker -- Tiger Trade)

- Wrap the official `tigeropen` SDK rather than hand-roll RSA request
  signing.
- Tiger Trade (real equities) over Tiger CFD for this round.
- Connectivity + account state only -- Tiger's order lifecycle gets its
  own design round later. See `DECISIONS.md`, ADR-0030.

### Verified (Fourth Broker -- Tiger Trade)

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **305
  passed**, 0 failed.

### Sprint 5 close-out

Sprint 5 is complete: four concrete brokers (`AlpacaBroker`,
`IBKRBroker`, `IGBroker`, `TigerBroker`) implement `BrokerConnection`
to varying depths (connectivity for all four; order submission and
cancellation for Alpaca; synchronous order submission for IG), plus
`src/reconciliation/` comparing simulated fills against real ones.
305/305 tests passed on the dev machine at close-out.

Before Sprint 6 could start, the four architectural inconsistencies
this sprint's work had introduced were treated as a **gate, not an
optional cleanup**: see "Pre-Sprint 6 -- Architecture Review Cleanup"
above (`DECISIONS.md`, ADR-0031 through ADR-0034). That gate is now
closed -- the combined Sprint 3/4/5 + cleanup suite stands at **329
tests, confirmed passing via real `pytest`** on the dev machine (Python
3.14.6). Sprint 6 does not begin against a suite with any known
architectural debt from Sprint 5 left uncorrected.

## Sprint 4 -- 2026-09-10, End-to-End Proof (feature-complete)

### Added (End-to-End Proof)

- `tests/test_integration_paper_trading.py` -- 5 tests proving
  `EMACrossStrategy` -> `PositionSizer` -> `PaperBroker` compose
  correctly end to end, driven by a real strategy's signals over real
  candles rather than constructed test objects: full pipeline runs
  without error; the first opening position is sized at exactly the
  configured fraction of starting equity; closing a position realizes
  P&L into cash matching the actual fill prices (derived from the
  fills themselves, not a hand-predicted EMA crossover value); a
  signal sized *after* a completed round trip uses the account's
  updated post-trade equity, not the original starting value -- the
  key proof that the `PositionSizer` <-> `PaperBroker` loop actually
  closes; `Signal.id`/`timestamp` remain traceable through every
  `Fill`. No new production code -- `src/risk` and `src/execution`
  remain standalone modules (ADR-0021/ADR-0022); this only proves
  wiring them together works, the way a future caller eventually will.

### Verified (End-to-End Proof)

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **197
  passed**, 0 failed. This closes out Sprint 4's remaining ROADMAP item
  -- Sprint 4 is now feature-complete and fully confirmed.

## Sprint 4, Paper Execution -- 2026-09-10

### Added (Paper Execution)

- `src/execution/` -- `PaperBroker` (`DECISIONS.md`, ADR-0022), touching
  zero existing files (Extension Cost: 0):
  - `engine.py` -- `PaperBroker.submit_signal(signal, symbol,
    fill_price, sizing_decision=None) -> Fill`. Translates a
    `LONG`/`SHORT` signal into a `BUY`/`SELL` order requiring an
    approved `SizingDecision`; a `FLAT` signal into whichever side
    closes the existing position, needing no `SizingDecision` at all.
    Fills instantly and completely at the caller-supplied price -- no
    slippage or commission. One open position per symbol at a time --
    opening a second raises rather than averaging into it.
    `account_state` returns a real `src.risk.AccountState`: `equity` is
    cash plus each position's *signed* value at entry price (correctly
    netting out a short's liability); `open_exposure` is the sum of
    unsigned cost basis. Not marked to market -- an open position's
    contribution to equity is frozen at its entry price until closed.
  - `models.py` -- `OrderSide` (`BUY`/`SELL`), `Order` (validated
    `quantity > 0`, traceable via `signal_id`/`timestamp` back to its
    originating `Signal`), `Fill` (`order`, `fill_price`,
    `cash_delta`), `Position` (signed `quantity`, `entry_price`,
    `entry_signal_id`).
  - This is the first module verified directly against `src/risk`
    (tests construct `SizingDecision`s and feed them to `PaperBroker`)
    -- closes the loop ADR-0021 left open. Not yet wired into
    `Backtester` or a real strategy loop.
- `tests/test_execution.py` -- 18 tests: opening/closing long and
  short (cash accounting, realized P&L on close), rejecting a second
  position in an already-open symbol, rejecting an unapproved or
  missing `SizingDecision`, rejecting a `FLAT` close with nothing open,
  input validation (`starting_cash`, `fill_price`, `Order.quantity`),
  `account_state` correctness immediately after opening (equity
  unchanged) and after closing (realized P&L reflected), traceability
  from `Fill` back to the originating `Signal`, defensive copy of
  `positions`.

### Decided (Paper Execution)

- Built the fuller version this round: real simulated fills and a
  tracked portfolio, not just `Signal` -> `Order` translation --
  explicitly asked and confirmed, since it closes ADR-0021's loop
  instead of deferring it further. See `DECISIONS.md`, ADR-0022.
- Cash accounting is uniform by order side (`BUY` pays cash out, `SELL`
  brings cash in) rather than branching on long/short -- this is what
  makes shorts "just work" without special-casing.
- No mark-to-market: equity reflects entry-price valuation until a
  position closes and P&L is realized into cash. `PaperBroker` has no
  price feed of its own, matching `PositionSizer`'s posture.

### Verified (Paper Execution)

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **192
  passed**, 0 failed.

## Sprint 4, Position Sizing -- 2026-09-10

### Added (Position Sizing)

- `src/risk/` -- `PositionSizer` (`DECISIONS.md`, ADR-0021), touching
  zero existing files (Extension Cost: 0):
  - `engine.py` -- `PositionSizer.size(signal, account, price) ->
    SizingDecision`. Sizes a `LONG`/`SHORT` signal at a **fixed**
    fraction of account equity (`RiskLimits.risk_per_trade_pct`,
    default 10%), applied identically regardless of `Signal.confidence`.
    Sizes down to whatever portfolio exposure headroom remains
    (`RiskLimits.max_portfolio_exposure_pct`, default 50% of equity)
    rather than rejecting outright when the full allocation doesn't
    fit; only rejects (`approved=False`) when there's no headroom left
    at all. Raises on a `FLAT` signal (nothing to size) or a
    non-positive price.
  - `models.py` -- `RiskLimits` (validated percentages, both in
    `(0, 1]`), `AccountState` (`equity`, `open_exposure`, validated
    positive/non-negative), `SizingDecision` (`approved`,
    `position_size`, `capital_allocated`, `reason`).
  - Deliberately **standalone** from `Backtester` this round --
    `Backtester` keeps ADR-0011's single-unit execution model
    untouched; wiring `PositionSizer` into a backtest or the future
    `src/execution` is a deliberate future step, not built here.
- `tests/test_risk.py` -- 16 tests: `RiskLimits`/`AccountState`
  validation (boundary values, out-of-range rejection), input
  validation on `size()` (`FLAT` signal, non-positive price), full-size
  allocation, sized-down allocation when headroom is partially used,
  rejection when the exposure limit is already reached or exceeded,
  `LONG`/`SHORT` sized identically, default `RiskLimits` behavior.

### Decided (Position Sizing)

- Fixed fraction of equity per trade, not confidence-scaled -- no
  validated relationship yet between a strategy's confidence score and
  how much capital it should be trusted with. See `DECISIONS.md`,
  ADR-0021.
- Portfolio-level limit is a percentage-of-equity cap, not a
  position-count cap -- answers "how much of the account is at risk
  right now" directly, regardless of how many positions that capital
  is split across.
- `PositionSizer` stays standalone from `Backtester` this round, so a
  regression can't be ambiguous between new sizing logic and the
  existing backtest engine it would otherwise get wired into in the
  same change.

### Verified (Position Sizing)

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **174
  passed**, 0 failed.

## Sprint 4, EMACrossStrategy -- 2026-09-09

### Added

- `src/strategies/ema_cross.py` -- `EMACrossStrategy`, the platform's
  first permanent strategy: long while EMA(fast) > EMA(slow), flat
  otherwise, long-only. Deliberately simple by design, not tuned for
  profitability -- carried forward from Sprint 3's roadmap note (see
  `ROADMAP.md`). Defaults `fast=12, slow=26`; raises `ValueError` if
  `fast >= slow`. `confidence` is a fixed, configurable constant per
  instance (no natural continuous confidence measure for a strategy
  this simple). Exported from `src/strategies/__init__.py`.
  Extension Cost (ADR-0014): 1 file changed outside the new file itself
  (`src/strategies/__init__.py`, one export added) -- as close to the
  "add a strategy, touch nothing else" ideal as the package's own
  `__all__` convention allows.
- `tests/test_ema_cross_strategy.py` -- 11 tests: constructor
  validation, `prepare()` output matches `IndicatorEngine` directly,
  no input mutation, sparsity (no two consecutive signals share a
  direction), never emits `SHORT`, EMA's lack of NaN warmup documented
  explicitly (its `.ewm(adjust=False)` is defined from row one -- the
  strategy's `pd.isna()` guard is defensive, not load-bearing, for this
  particular indicator), confidence is fixed and configurable, signal
  metadata includes `strategy`/`reason`, end-to-end through the real
  `Backtester` with default periods.

### Changed

- `tests/test_strategy_sdk.py` -- its local `EMACrossStrategy` demo
  class was removed in favor of importing the real
  `src/strategies/ema_cross.EMACrossStrategy`, so the SDK-integration
  tests and the strategy's own logic tests share one implementation
  instead of two copies that could quietly drift apart.

### Verified

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **158
  passed**, 0 failed.

## Sprint 3 -- 2026-09-09

### Added (Module 4 -- AI Research Reporter)

- `src/research/` -- the AI Research Reporter (`DECISIONS.md`,
  ADR-0020):
  - `compiler.py` -- `compile_findings(result, attribution) ->
    ResearchFindings`. Fully deterministic: extracts trade count, win
    rate, Sharpe, max drawdown, average hold, and best/worst regime as
    `Finding(label, value)` pairs, plus an optional single
    `recommendation` string produced only when the worst regime bucket
    actually has a negative average return. Never reasons about
    session-of-day timing -- that axis doesn't exist yet (ADR-0019).
  - `renderers.py` -- `NarrativeRenderer` protocol,
    `FallbackNarrativeRenderer` (template-based prose, zero setup,
    always available), `ClaudeNarrativeRenderer` (calls the Claude API
    with a system prompt that forbids stating any fact not already in
    the findings). `anthropic` is a lazy, optional import -- not added
    to `requirements.txt`.
  - `reporter.py` -- `ResearchReporter.run(result, attribution) ->
    ResearchReport`. Picks `ClaudeNarrativeRenderer` when
    `ANTHROPIC_API_KEY` is set and `anthropic` is importable,
    `FallbackNarrativeRenderer` otherwise; falls back to the
    deterministic renderer on any LLM-path exception rather than losing
    the report.
  - `models.py` -- `Finding`, `ResearchFindings`, `ResearchReport`.
- `src/utils/formatting.py` (new) -- `format_timedelta`, promoted out of
  `src/attribution/models.py`'s private `_format_timedelta` so
  `AttributionReport.report()` and `compile_findings()` share one
  implementation (same precedent as `CacheManager`, ADR-0008).
- `tests/test_research.py` -- 15 tests: findings extraction (present/
  absent metrics, formatting, recommendation logic, explicit check that
  session-of-day is never mentioned), both renderers (fallback prose,
  a mocked Claude call asserting the system prompt and findings text
  sent), and `ResearchReporter` (fallback selection, injected renderer,
  fallback-on-exception, findings always populated).

### Changed (Module 4)

- `src/attribution/models.py` -- removed the private `_format_timedelta`,
  now imports and uses `src/utils/formatting.format_timedelta`.
- `src/utils/__init__.py` -- exports `format_timedelta` alongside
  `CacheManager`.
- `src/cli/checks.py` -- `check_api_keys` upgraded from `NOT_IMPLEMENTED`
  to a real, always-`OK` check reporting whether `ANTHROPIC_API_KEY` is
  set. Its absence doesn't degrade platform health -- research reports
  always work via the fallback renderer.

### Decided (Module 4)

- Hybrid mechanism: deterministic findings compiler + optional LLM
  rendering pass that only rephrases, never invents. See `DECISIONS.md`,
  ADR-0020.
- Strictly evidence-grounded: the compiler only reasons about metrics
  the platform already computes (regime breakdown), and explicitly
  never fabricates a session-of-day claim, since that attribution axis
  is still deferred pending ADR-0006.
- `src/research/` is distinct from the Sprint 7+ `src/ai/` scope: this
  module produces a research recommendation for a person to weigh
  (ADR-0017), never a trading signal.

### Verified (Module 4)

- Confirmed via real `pytest` on the dev machine (Python 3.14.6): **147
  passed**, 0 failed. This closes Sprint 3 -- all four modules verified
  end to end.

### Added (Module 3 -- Performance Attribution)

- `src/attribution/` -- `PerformanceAttributor` (`DECISIONS.md`,
  ADR-0019), touching zero existing files (Extension Cost: 0):
  - `engine.py` -- `PerformanceAttributor.run(result, candles,
    **regime_kwargs) -> AttributionReport`. Independently recomputes
    regime via `MarketRegimeEngine(candles)` rather than reading
    `Signal.metadata`, so attribution works for every backtest
    regardless of what a strategy recorded. Buckets each trade by the
    regime (trend + volatility only, risk axis excluded) at its
    `entry_time`; a trade entering during indicator warmup is bucketed
    `"unknown"` and excluded from "Best"/"Worst Regime".
  - `models.py` -- `AttributionReport` (trades, winning trades, win
    rate, average hold, regime breakdown, best/worst regime, plus
    `.report()`), `RegimeStats`.
- `tests/test_attribution.py` -- 8 tests: counts/win rate, average
  hold (mean duration + unit-appropriate formatting), regime label
  correctness cross-checked directly against `MarketRegimeEngine`'s own
  output (not hardcoded), warmup-period trades excluded from best/worst,
  best/worst reflecting engineered returns across two distinct regimes,
  empty-trades neutral report.

### Decided (Module 3)

- Regime attribution uses the regime **at trade entry**, not the
  dominant regime across the whole hold -- simpler, matches "what
  conditions was this decision made in." See `DECISIONS.md`, ADR-0019.
- Session-of-day attribution (morning/lunch/power hour) is deliberately
  **not** included this round -- it depends on candle timestamps being
  in market-local time, which ADR-0006 (timezone consistency) hasn't
  landed. Shipping it now would mean an unverified assumption baked
  into a headline number; held off instead. See `DECISIONS.md`,
  ADR-0019.
- `src/attribution` is distinct from the planned `src/analytics`
  (Sprint 6): attribution explains one backtest, analytics is
  cross-experiment/live tracking. Noted in `ARCHITECTURE.md` so the two
  don't quietly duplicate each other later.

### Verified (Module 3)

- New suite verified in sandbox: 8/8 `test_attribution.py` tests pass.
  Full project suite: 129/131 in sandbox (2 known environment-only
  failures, consistent with prior sessions). Real-machine confirmation
  came alongside Module 4's -- see "Verified (Module 4)" above: all 147
  tests, including these 8, passed via real `pytest`.

### Added (Module 2 -- Strategy SDK)

- `src/strategies/sdk.py` -- `BaseStrategy` (`DECISIONS.md`, ADR-0018):
  an optional `ABC` handling setup boilerplate only. Provides `name`/
  `self.log` (bound logger), `self.indicator(data, name, **params)`
  (stateless `IndicatorEngine` wrapper), `self.require_columns(data,
  *columns)` (defaults to OHLCV, raises a clear strategy-attributed
  error), `self.emit_signal(timestamp, direction, confidence,
  **metadata)` (constructs + logs a `Signal`, merges in `{"strategy":
  self.name}`). `prepare()`/`generate_signals()` remain abstract -- the
  author writes both in full, including when/how often to emit.
  Extension Cost: 0 existing files to add a new SDK-based strategy.
- `tests/test_strategy_sdk.py` -- 11 tests: abstractness enforcement,
  each helper in isolation, and `EMACrossStrategy(BaseStrategy)` run
  end-to-end through the real `Backtester`, including reuse of one
  strategy instance across two different candle sets (proving
  `self.indicator()` holds no stale state).

### Decided (Module 2)

- Rejected an opinionated design where the SDK's base class would own
  the `generate_signals` loop and ask the author for a single
  vectorized per-bar decision function -- a strategy author should
  never be reduced to one method, and the SDK should never decide when
  a signal fires. See `DECISIONS.md`, ADR-0018.
- `Strategy` (the Protocol, ADR-0011/0015) is unchanged; `BaseStrategy`
  is one optional way to satisfy it, not a requirement.

### Verified (Module 2)

- Full suite confirmed on the real dev machine: `pytest` -> **123
  passed** in 0.43s (11 backtesting + 6 cache + 26 cli/doctor + 7
  config + 16 experiments + 11 indicators + 15 market data + 10 regime
  + 10 signals + 11 strategy_sdk). Strategy SDK module complete.

### Added (Module 1 -- Signal Framework)

- `src/signals/` -- the Signal Framework, Sprint 3 Module 1 and one of
  the platform's foundational contracts (see `DECISIONS.md`, ADR-0015,
  supersedes ADR-0011):
  - `models.py` -- `Signal` (frozen dataclass: `timestamp`, `direction`,
    `confidence` validated to `[0.0, 1.0]`, `metadata`, `id: UUID`
    assigned client-side) and `SignalDirection` (`LONG`/`SHORT`/`FLAT`).
    No `price` field, deliberately -- a Signal is a decision, not a
    market event or an order.
- `tests/test_signals.py` -- 10 tests: confidence validation at and
  past both boundaries, immutability, id uniqueness/reconstruction,
  metadata defaults, confirming no `price` attribute exists.
- `src/strategies/base.py` -- `Strategy.generate_signals()` now returns
  `list[Signal]` (sparse -- one per decision point, not one per
  candle), replacing the `SIGNAL_COLUMN`/DataFrame contract entirely.
- `src/backtesting/models.py` -- `Trade` now references `entry_signal_id`
  / `exit_signal_id: UUID | None` instead of embedding `Signal` objects.
  `BacktestResult` gained a `signals: list[Signal]` field.
- `src/backtesting/engine.py` -- rewritten to consume a sparse
  `list[Signal]`: holds each signal's direction from its own bar
  forward until the next signal, then applies ADR-0011's no-lookahead
  shift once when computing the equity curve. A `Trade` now falls
  directly out of two consecutive signals rather than being inferred
  by diffing a dense column. `Backtester.run()` raises `ValueError` if
  `generate_signals()` doesn't return a `list[Signal]`.
- `src/experiments/registry.py` -- new `signals` table plus
  `save_signals(experiment_id, signals)`, `get_signals(experiment_id)`,
  `get_signal(signal_id)` (see `DECISIONS.md`, ADR-0016: signals are
  stored here, not in a separate repository). Added as new methods,
  not a new parameter on `log_experiment()` -- its signature and every
  existing test against it are unchanged.
- `tests/test_backtesting.py` -- rewritten for the new contract: 11
  tests covering signal-id linkage on both sides of a trade, redundant
  same-direction signals not splitting a trade, a signal referencing a
  timestamp outside the backtest's candles being skipped rather than
  crashing, and the existing no-lookahead/metrics/report coverage
  carried forward.
- `tests/test_experiments.py` -- 6 new tests for signal storage
  (round-trip, ordering by timestamp, isolation between experiments,
  direct lookup by id, persistence across reconnects); all 10 original
  tests unchanged.

### Decided (Module 1)

- Standardized `Signal` as `timestamp`/`direction`/`confidence`/
  `metadata`/`id`, with `SignalDirection` = `LONG`/`SHORT`/`FLAT` (not
  `BUY`/`SELL`) and no `price` field -- a Signal answers "what position
  should the portfolio move toward," not "at what price." See
  `DECISIONS.md`, ADR-0015.
- Signals are emitted sparsely (one per decision point) rather than
  densely (one per candle), since per-signal metadata like "reason"
  is only accurate at the moment a decision is made. See ADR-0015.
- `Trade` references signals by `UUID` rather than embedding them, and
  the Experiment Registry -- not a new dedicated repository -- owns
  actual `Signal` storage. See `DECISIONS.md`, ADR-0016.
- This is a supersession of Sprint 2's ADR-0011, not a pure addition --
  flagged explicitly before implementation started, since it touches
  already-shipped, tested code (`src/strategies`, `src/backtesting`).
- Adopted a second standing engineering principle alongside ADR-0000:
  every component produces knowledge for the next component, not a
  finished decision on its behalf (data -> features -> signals ->
  evidence -> a record -> a recommendation). See `DECISIONS.md`,
  ADR-0017.

### Verified (Module 1)

- Full suite confirmed on the real dev machine: `pytest` -> **112
  passed** in 0.72s (11 backtesting + 6 cache + 26 cli/doctor + 7
  config + 16 experiments + 11 indicators + 15 market data + 10 regime
  + 10 signals). Signal Framework module complete.

## Pre-Sprint 3 -- 2026-07-28

### Added

- `src/cli/` -- `atp doctor`, a full system health check (see
  `DECISIONS.md`, ADR-0013):
  - `registry.py` -- `@register_check` decorator + lookup, the same
    pattern as `src/indicators/registry.py`.
  - `checks.py` -- Python Version, Configuration, Market Data (a real,
    cache-bypassing fetch), Cache (round-trips a throwaway key through
    the real cache dir), Experiments DB (queries the real SQLite file).
    Broker Connection and API Keys report `NOT_IMPLEMENTED` rather than
    being omitted or faked as passing, since `src/broker` doesn't exist
    yet and the current provider needs no key.
  - `doctor.py` -- runs every registered check, prints the report,
    computes the exit code (`0` healthy, `1` on any failure).
  - `__main__.py` -- `python -m src.cli doctor`.
- `tests/test_cli_doctor.py` -- 26 tests: registry behavior, every
  check in isolation (network/cache/DB faked out so the suite stays
  network-free), doctor report formatting for all-pass/some-failed/
  some-not-implemented, and command dispatch.
- `DECISIONS.md`, ADR-0000 -- the project's architectural north star,
  stated explicitly for the first time: "every new market, indicator,
  strategy, broker, or AI model should be added as an extension -- not
  require a rewrite of existing code." Numbered 0000, not 0001, since
  ADRs are append-only and 0001 (capability-based `src/`) already
  existed; 0000 marks it as the premise the others were already
  following.

### Verified

- Full suite confirmed on the real dev machine: `pytest` -> **93
  passed** (67 prior + 26 for `atp doctor`). `python -m src.cli doctor`
  run for real: all five implemented checks pass, Broker Connection and
  API Keys correctly report not-yet-implemented, exit code 0.

## Sprint 2 (closed 2026-07-28) -- 2026-07-21

### Added

- `src/indicators/` -- the Indicator Engine, Module 1 of the Sprint 2
  research engine (see `DECISIONS.md`, ADR-0009):
  - `registry.py` -- `@register_indicator` decorator + lookup; the only
    mechanism for adding a new indicator.
  - `formulas.py` -- SMA, EMA, RSI, ATR, MACD, VWAP as pure functions.
  - `engine.py` -- `IndicatorEngine(candles).calculate(name, **params)`,
    the public facade; validates OHLCV columns at construction.
- `tests/test_indicators.py` -- 11 tests, each formula checked against
  an independent reference calculation (a plain Python loop) rather
  than restating the pandas expression, so a vectorization bug would
  actually be caught.
- `src/regime/` -- the Market Regime Engine, Module 2. Scores every
  candle against all six named regimes (trending/ranging,
  volatile/low_volatility, risk_on/risk_off) as continuous `[0, 1]`
  scores; risk axis is `NaN` until a VIX/macro data source exists. See
  `DECISIONS.md`, ADR-0010.
- `tests/test_regime.py` -- 10 tests against deterministic synthetic
  price series (flat, steady uptrend, narrow-then-wide volatility) so
  expected outcomes are reasoned about exactly.
- `src/strategies/base.py` -- the `Strategy` interface (`name`,
  `prepare()`, `generate_signals()`) as a `typing.Protocol`, plus
  `SIGNAL_COLUMN`. No concrete strategy yet (Sprint 3) -- this is the
  seam the backtester runs against. See `DECISIONS.md`, ADR-0011.
- `src/backtesting/` -- the Backtesting Framework, Module 3:
  `Backtester.run()`, `Trade`/`BacktestResult` (`models.py`),
  `calculate_metrics()`/`sharpe_ratio()`/`max_drawdown()` (`metrics.py`).
  Simplified execution model (single unit size, no costs/slippage,
  no-lookahead position shift). See `DECISIONS.md`, ADR-0011.
- `tests/test_backtesting.py` -- 8 tests, including hand-verified trade
  extraction/equity curve via a `ScriptedStrategy` test double, plus an
  end-to-end run of a real SMA-crossover strategy using `IndicatorEngine`.
- `src/experiments/` -- the Experiment Registry, Module 4:
  `ExperimentRegistry` (SQLite-backed, stdlib `sqlite3`), `Experiment`
  dataclass with a `.summary()` view. Decoupled from `src/backtesting` --
  stores plain dicts, not `BacktestResult` objects. See `DECISIONS.md`,
  ADR-0012.
- `tests/test_experiments.py` -- 10 tests: CRUD round-trip, decision
  validation, filtering by decision/strategy, persistence across
  reconnects, summary rendering.

### Decided

- Pivoted Sprint 2 from a narrow "build indicators" scope into a
  four-module research engine (Indicator Engine, Market Regime
  Detection, Backtesting Framework, Experiment Registry) before any
  real strategy is built. See `DECISIONS.md`, ADR-0009.
- Regime scoring is continuous (`[0, 1]` per regime) and exposes all
  six regime names immediately, with the risk axis returning `NaN`
  until a real signal exists, rather than omitting risk_on/risk_off
  from the interface until later. See `DECISIONS.md`, ADR-0010.
- `Strategy` is a `Protocol` (structural typing), and the Backtester
  uses a deliberately simplified execution model (no costs, no
  slippage, single unit size) -- realistic execution is explicitly
  deferred to `src/risk`/`src/execution` in Sprint 4. See
  `DECISIONS.md`, ADR-0011.
- Experiment Registry uses SQLite over a file-per-experiment scheme,
  prioritizing fast filtering/counting at hundreds of rows over git
  diffability. See `DECISIONS.md`, ADR-0012.

### Verified

- Full suite confirmed on the real dev machine: `pytest` -> **67
  passed** in 1.47s (8 backtesting + 6 cache + 7 config + 10
  experiments + 11 indicators + 15 market data + 10 regime). Sprint 2
  is closed.

## Sprint 1 (in progress) -- 2026-07-21

### Added

- `src/config/settings.py` -- `PROJECT_ROOT`, `DATA_DIR`, `LOG_DIR`,
  `CONFIG_DIR`, `WATCHLIST = ["SPY", "QQQ"]`, `DEFAULT_PERIOD = "2y"`,
  `DEFAULT_INTERVAL = "5m"`.
- `src/config/logging.py` -- shared `loguru` logger: rotating file sink
  (`logs/trading.log`, 10 MB rotation, 30-day retention) + console sink.
- `src/main.py` -- thin application entrypoint; logs startup and watchlist.
- `MarketDataService.get_history()` -- period-based fetching (e.g.
  `"5d"`, `"3mo"`, `"2y"`) as an alternative to explicit `start`/`end`
  dates on `get_candles()`. Defaults pulled from `config.settings`.
- `tests/test_config.py` -- 7 tests covering settings values, directory
  creation, and logger behavior (console + file sink).
- 7 new tests in `tests/test_market_data.py` covering `period_to_start()`
  parsing (days/weeks/months/years, malformed input) and `get_history()`.
- `PROJECT_STATE.md`, `ARCHITECTURE.md`, `CHANGELOG.md`, `DECISIONS.md`,
  `ROADMAP.md` -- the project's standing documentation set.
- `src/utils/cache.py` -- `CacheManager`, a generic key -> DataFrame
  on-disk cache extracted out of `MarketDataService`, so future
  capabilities (news, options chains, VIX, macro data, earnings, forex)
  can reuse it instead of reimplementing caching. `MarketDataService`
  now depends on it instead of doing its own file I/O. See
  `DECISIONS.md`, ADR-0008.
- `tests/test_cache.py` -- 6 tests against `CacheManager` directly, no
  market-data references at all.

### Decided

- Extended the existing `MarketDataService` rather than creating a
  second, simpler one, when an external lesson plan proposed a
  duplicate `src/data/market_data.py`. See `DECISIONS.md`, ADR-0003.
- Logged four forward-looking decisions without implementing them this
  sprint: package layout migration to `src/ai_trading_platform/`
  (ADR-0004, pre-1.0), typed `Settings` object (ADR-0005, pre-1.0),
  data validation layer (ADR-0006), and incremental cache fetch
  (ADR-0007). All four are tracked in `DECISIONS.md` and cross-referenced
  from `ROADMAP.md` / `PROJECT_STATE.md` so the direction isn't lost.
- Split caching out of `MarketDataService` into `CacheManager`
  (ADR-0008) -- done immediately this sprint, not deferred, since it
  was flagged as blocking before Sprint 1 closes.

### Verified

- Full test suite run on the real dev machine: `pytest` -> **22 passed**
  (7 config + 15 market data). `python src/main.py` confirmed logging
  both to console and `logs/trading.log`.
- Local git repository initialized; first commit made (33 files, "Initial
  project setup"). Remote added (`github.com/grow-op-nasa/ai-trading-platform`)
  and pushed; `main` now tracks `origin/main`.
- CacheManager extraction verified in sandbox: all 15 `MarketDataService`
  tests pass unmodified against the new `CacheManager`-backed
  implementation, plus 6 new `CacheManager` tests. Full suite now 28
  tests; pending final confirmation via real `pytest` run.

## Sprint 0 -- 2026-07-21

### Added

- macOS development environment: Homebrew, Git 2.50.1, Python 3.14.6,
  VS Code.
- Project scaffold at `~/Projects/ai-trading-platform`, git-initialized.
- `src/` organized by capability (`data`, `indicators`, `strategies`,
  `broker`, `execution`, `risk`, `analytics`, `ai`, `dashboard`,
  `utils`) rather than by strategy -- see `DECISIONS.md`, ADR-0001.
- Python virtual environment (`.venv`) with initial dependencies:
  pandas, numpy, yfinance, plotly, streamlit, vectorbt, python-dotenv,
  rich, loguru, pytest, jupyter. Frozen to `requirements.txt`.
- Market Data Service v1: `DataProvider` interface + `Interval` enum
  (`src/data/base.py`), Yahoo Finance implementation
  (`src/data/yfinance_provider.py`), public facade with CSV caching
  (`src/data/service.py`), and distinct `DataProviderError`/`NoDataError`
  exceptions (`src/data/exceptions.py`). See `DECISIONS.md`, ADR-0002.
- `tests/test_market_data.py` -- 8 tests against a `FakeProvider`
  double, no network dependency.
- `.gitignore`, `pyproject.toml` (pytest config, ruff config), `README.md`.
