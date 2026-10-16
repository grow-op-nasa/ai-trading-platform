"""Deterministic, paper-only trading runtime (Sprint 18, ADR-0053).

Composes the existing components -- `MarketDataService`, the strategy registry, `PortfolioRiskEngine`,
`ExecutionModel`, `PaperBroker`, the experiment registry's paper portfolio, and the monitoring service --
into one bounded, auditable decision cycle. Importing this package starts nothing; a cycle runs only when an
operator calls `PaperTradingRuntime.run_cycle()` (CLI: `python -m src.cli paper run --once`).

It is hard-bound to `PaperBroker`: it imports no live broker adapter, has no broker factory, and no LLM.
"""
