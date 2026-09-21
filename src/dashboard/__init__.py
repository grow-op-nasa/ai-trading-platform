"""The Streamlit research and paper-portfolio dashboard -- Sprint 9
(`DECISIONS.md`, ADR-0042).

    streamlit run src/dashboard/app.py

A read-only interface over the platform's existing backtesting,
experiment, strategy, market-data, and portfolio capabilities -- it is
not a production trading terminal, and it cannot place an order,
change a risk limit, or mutate any `Portfolio`/`Experiment`/`Strategy`
state (`tests/test_architecture.py` enforces the dependency direction:
this package depends on `src.analytics`, never the reverse, and never
imports `yfinance` or bypasses `MarketDataService` for a market price).

See `src.dashboard.app` for the entrypoint, `src.dashboard.views` for
the four pages (Overview, Backtest/Experiment Analysis, Strategy
Comparison, Paper Portfolio), and `src.dashboard.formatting` for the
presentation-only number formatting shared across them.
"""
