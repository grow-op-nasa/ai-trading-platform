# Changelog

All notable changes to this project are documented here, grouped by
sprint. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## Sprint 2 (in progress) -- 2026-07-21

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
