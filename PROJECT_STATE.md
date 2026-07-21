# Project State

_Last updated: 2026-07-21 (Sprint 1, Module 2)_

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

## Current Module

**Market Data Service**

Status: **~90%**

What's done: `DataProvider`/`Interval` abstraction, Yahoo Finance
provider, CSV caching, both date-range (`get_candles`) and period-based
(`get_history`) entry points, 22 passing tests (0 network-dependent).

What's left: historical data caching is CSV-only and re-fetches whole
ranges on any cache-key miss (no incremental/append caching yet); no
integration test suite that hits the real yfinance API on a schedule.

## Next Task

Historical data caching improvements -- specifically, incremental
caching so a repeated request with a shifted date range doesn't refetch
data already on disk. After that: `src/indicators/` (first indicator:
moving average, built directly on `MarketDataService`).

## Known Issues

- None blocking. `src/data/market_data.py` was proposed by an external
  lesson plan as a second, simpler `MarketDataService`; we decided
  (see `DECISIONS.md`, ADR-0003) to extend the existing service instead,
  so that file does not exist and is not needed.

## Technical Debt

- Cache is CSV, not Parquet -- fine at current data volumes, but slower
  and larger on disk once indicators/backtests pull years of intraday
  data. Revisit once `src/indicators/` exists and we can measure it.
- `loguru`'s default console format (timestamp + level + file:line) is
  noisier than a human-facing CLI probably wants long-term; not worth
  tuning yet since nothing consumes the console output programmatically.
- No CI (GitHub Actions or similar) running the test suite on push yet.

## How to verify this file is accurate

```bash
pytest                 # should show 22 passed
python src/main.py     # should log startup + watchlist
```
