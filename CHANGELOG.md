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

## Sprint 4 (in progress) -- 2026-09-10

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
