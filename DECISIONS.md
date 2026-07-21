# Architecture Decision Records

Each entry captures a decision, the context that led to it, and what it
costs/buys us. New decisions are appended, never rewritten -- if a
decision is later reversed, add a new ADR that supersedes it rather than
editing the old one.

---

## ADR-0001: Organize `src/` by capability, not by strategy

**Status:** Accepted -- Sprint 0

**Context:** Most solo trading-bot projects organize code as
`strategies/mean_reversion/`, `strategies/momentum/`, etc., each
strategy owning its own data-fetching, sizing, and execution code.

**Decision:** Organize `src/` by capability instead: `data`,
`indicators`, `strategies`, `broker`, `execution`, `risk`, `analytics`,
`ai`, `dashboard`, `utils`. Strategies become thin consumers of the
other capabilities rather than each reimplementing them.

**Consequences:** Adding a new strategy six months from now should mean
writing logic that consumes `data` + `indicators` and produces signals
for `risk`/`execution` -- not rebuilding a data pipeline. The cost is
more upfront structure (ten near-empty packages in Sprint 0) before any
single strategy exists. Notably, `ai` is one capability among many, not
the center of the project -- an AI-generated signal is meant to be
swappable for a rule-based one without touching risk, execution, or the
dashboard.

---

## ADR-0002: `DataProvider` abstraction behind `MarketDataService`

**Status:** Accepted -- Sprint 0

**Context:** The only market data vendor in use today is Yahoo Finance
(via `yfinance`), which is free but has known limitations (rate limits,
limited intraday history). A future move to Polygon, Interactive
Brokers, or another vendor is likely.

**Decision:** Define an abstract `DataProvider` interface
(`fetch_candles(symbol, start, end, interval) -> DataFrame` with a
guaranteed column/index contract) in `src/data/base.py`.
`YFinanceProvider` is the only implementation today.
`MarketDataService` depends on the interface, not the implementation,
and takes a `provider` in its constructor. Caching (CSV, keyed by
symbol/interval/date-range) lives in the service layer, not the
provider, so it applies uniformly regardless of vendor. Errors are
split into `DataProviderError` (the provider failed) and `NoDataError`
(valid request, empty result) because callers generally want to handle
"the vendor is down" differently from "there's no data for this range."

**Consequences:** Switching vendors means writing one new class and
passing it to `MarketDataService(provider=...)` -- no changes to
strategies, backtests, or the dashboard. The cost is a small amount of
indirection (an abstract base class and an enum) for a project that, at
Sprint 0, has exactly one provider to abstract over.

---

## ADR-0003: Extend `MarketDataService` rather than duplicate it

**Status:** Accepted -- Sprint 1

**Context:** An external lesson plan (followed alongside this build)
specified creating `src/data/market_data.py` with its own
`MarketDataService` class (`get_history(ticker, period, interval)` via
raw `yf.download`), and overwriting `tests/test_market_data.py` with a
script that prints output and hits the network on every run. By this
point the project already had a more complete `MarketDataService` in
`src/data/service.py` (the one specified in ADR-0002), with 8 passing
tests and no network dependency in the unit suite.

**Decision:** Do not create a second `MarketDataService`. Instead,
add period-based fetching (`get_history(symbol, period, interval)`) as
a new method on the existing `MarketDataService`, implemented as a thin
wrapper that converts the period string to a `start` date and delegates
to `get_candles()`. Defaults (`period`, `interval`) are pulled from
`src/config/settings.py`. The existing `tests/test_market_data.py` was
extended, not replaced.

**Consequences:** One class, one name, one test file -- no confusion
about which `MarketDataService` a given import refers to. The tradeoff
is that when following an external lesson plan verbatim would be
faster, this project prioritizes internal consistency and the
already-agreed four-criteria bar (works / tested / documented /
extensible) over matching the lesson plan's code line-for-line. Future
lesson content that proposes new modules should be checked against
what already exists before being applied verbatim.
