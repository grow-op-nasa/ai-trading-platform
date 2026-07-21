# Changelog

All notable changes to this project are documented here, grouped by
sprint. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

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

### Decided

- Extended the existing `MarketDataService` rather than creating a
  second, simpler one, when an external lesson plan proposed a
  duplicate `src/data/market_data.py`. See `DECISIONS.md`, ADR-0003.

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
