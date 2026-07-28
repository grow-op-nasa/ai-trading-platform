# Architecture Decision Records

Each entry captures a decision, the context that led to it, and what it
costs/buys us. New decisions are appended, never rewritten -- if a
decision is later reversed, add a new ADR that supersedes it rather than
editing the old one.

**Architectural north star (ADR-0000):** every new market, indicator,
strategy, broker, or AI model should be added as an extension -- not
require a rewrite of existing code. When a design decision is unclear,
the test is "does this make future extensions easier or harder?" If
harder, redesign.

**Second standing principle (ADR-0017):** every component produces
knowledge for the next component, not a finished decision on its
behalf -- data produces data, indicators produce features, strategies
produce signals, backtesting produces evidence, experiments produce a
record, AI produces a recommendation. When a design decision is
unclear, also ask: does this component's output stay knowledge the
next layer can use on its own terms, or does it sneak in a decision
that belongs downstream?

---

## ADR-0000: Extensibility is the architectural north star

**Status:** Accepted -- Sprint 2

**Context:** Sprint 0-2's biggest decisions -- capability-based `src/`
organization (ADR-0001), the `DataProvider` abstraction (ADR-0002), the
`CacheManager` split (ADR-0008), and building a research engine before
any strategy (ADR-0009) -- were each justified independently at the
time, but all share the same underlying instinct: don't make a future
addition require touching existing code. That instinct is worth stating
once, explicitly, as a standing principle, rather than re-deriving it
informally on every decision.

**Decision:** Adopt as the project's guiding principle: "Every new
market, indicator, strategy, broker, or AI model should be added as an
extension -- not require a rewrite of existing code." Whenever a design
decision is unclear, the test is "does this make future extensions
easier or harder?" If harder, redesign. This is numbered ADR-0000
rather than becoming the new ADR-0001, since ADRs are append-only and
never renumbered or edited retroactively (see this file's header) --
0000 sits before 0001 to mark it as the premise the others were already
following, not a rule invented after the fact.

**Consequences:** Every ADR from 0001 onward can be read as an
application of this principle, even though most of them predate its
number. Going forward, any proposed design that fails the "easier or
harder" test should be flagged and reworked before being accepted --
the same way ADR-0003 (rejecting a duplicate `MarketDataService`) and
ADR-0008 (the CacheManager split done immediately rather than deferred)
already were, before this principle had a name.

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

---

## ADR-0004: Migrate to `src/ai_trading_platform/` package layout

**Status:** Accepted, implementation deferred to pre-1.0 -- Sprint 1

**Context:** The current layout is `src/data/`, `src/config/`, etc.,
imported as `from src.data.service import MarketDataService`. `src`
itself is a bare directory, not an installable package with its own
name -- this works today because `pyproject.toml` sets
`pythonpath = ["."]` for pytest, but it's not the standard layout
(PEP 517/518 "src layout" expects `src/<package_name>/`). It's also the
root cause of an existing inconsistency: `src/main.py` imports as
`from config.settings import WATCHLIST` (relying on the script's own
directory being on `sys.path`), while every test and every module
inside `src/` imports as `from src.config.settings import ...`. Two
different import conventions for the same package is a real cost as
the module count grows toward the 30-40 mentioned in `ARCHITECTURE.md`.

**Decision:** Before v1.0, rename `src/` to `src/ai_trading_platform/`,
add proper packaging metadata to `pyproject.toml` (`[project]` /
`[tool.setuptools]` package discovery), and install the project in
editable mode (`pip install -e .`) so every module and script imports
consistently as `from ai_trading_platform.data import MarketDataService`
-- no more dual convention. Not done in Sprint 1: this touches every
import statement in the codebase, so it's deliberately scoped as a
single dedicated "flag day" change rather than something to interleave
with feature work.

**Consequences:** Until this migration happens, the two import styles
(`config.x` in `src/main.py`, `src.config.x` everywhere else) remain and
should not be treated as a bug -- it's known, accepted debt with a
tracked fix. When the migration happens: every `from src.X import Y`
becomes `from ai_trading_platform.X import Y`; `src/main.py` likely
becomes a console-script entry point instead of a path-dependent
script; the 22 existing tests (and however many exist by then) are the
regression safety net that makes the rename low-risk to execute in one
pass.

---

## ADR-0005: Typed `Settings` object instead of module-level constants

**Status:** Proposed -- Sprint 1 (timing not yet committed to a sprint)

**Context:** `src/config/settings.py` currently exposes flat
module-level constants (`WATCHLIST`, `DEFAULT_PERIOD`,
`DEFAULT_INTERVAL`, `PROJECT_ROOT`, etc.), imported directly by name
(`from src.config.settings import WATCHLIST`). This has no validation
(nothing stops `WATCHLIST` from being reassigned to a malformed value
at import time) and no clean way to support multiple environments
(development, paper trading, live trading) with different defaults --
today that would mean branching logic wherever a constant is read,
rather than one object that already reflects the active environment.

**Decision:** Eventually replace the flat constants with a typed
configuration object (candidates: a `pydantic.BaseSettings` subclass
for free env-var loading and validation, or a plain `dataclass` if we
want to avoid adding `pydantic` as a dependency), instantiated once as
a module-level `settings`, and accessed as `settings.watchlist`,
`settings.default_period`, etc. instead of importing constants by
name. Environment selection (development / paper trading / live
trading) becomes a matter of which values `settings` is constructed
with, likely driven by an `ENVIRONMENT` env var read from a `.env` file
(the `python-dotenv` dependency is already installed for this reason).

**Consequences:** Every current consumer of
`from src.config.settings import WATCHLIST` (etc.) needs to change to
`from src.config.settings import settings` + `settings.watchlist` --
a pervasive but mechanical refactor, best done together with or right
after ADR-0004's package rename since both touch the same import
surface. Buys real validation (a malformed watchlist or interval fails
fast at startup instead of surfacing as a confusing error deep in
`MarketDataService`) and clean environment support. Not started this
sprint; tracked here so the direction isn't lost.

---

## ADR-0006: Data validation layer for candle data

**Status:** Proposed -- Sprint 1

**Context:** Today, validation of fetched candles is limited to
`YFinanceProvider._normalize()` checking that the required columns
exist, deduplicating timestamps, and sorting the index -- and this only
runs on a fresh provider fetch. Data served from the CSV cache
(`MarketDataService.get_candles`, cache-hit path) is read straight off
disk with no re-validation at all. Neither path checks for timezone
consistency, negative prices, zero volume where it shouldn't occur
(e.g. during regular trading hours), or gaps in the expected timestamp
sequence. Bad market data of this kind doesn't raise an error -- it
just quietly feeds wrong numbers into whatever consumes it (indicators,
strategies, backtests), which is a far worse failure mode than a loud
crash.

**Decision:** Add a validation step (`src/data/validation.py`, likely a
`validate_candles(df) -> None` that raises a new `DataValidationError`
on failure) that `MarketDataService` runs on every DataFrame it
returns -- whether freshly fetched or served from cache. Checks to
include: timezone consistency (index is tz-naive, or consistently
tz-aware in one zone -- pick one and enforce it), no duplicate
timestamps, no unexplained gaps in the timestamp sequence relative to
the requested interval, no negative `open`/`high`/`low`/`close`, no
zero volume during regular trading hours, and a sorted-ascending index.

**Consequences:** Small validation cost on every call, negligible next
to a network fetch. The gap and zero-volume checks need a notion of
"expected trading session" (market holidays, weekends, regular hours)
that doesn't exist yet -- likely a minimal trading-calendar helper
needs to be built alongside this, or those two checks are scoped down
initially (e.g. flag gaps larger than N intervals rather than modeling
the full NYSE calendar) and tightened later. The payoff is that bad
data fails loudly and immediately instead of silently corrupting
strategies built on top of it later.

---

## ADR-0007: Incremental cache fetch (missing-range only) instead of whole-range refetch

**Status:** Proposed -- Sprint 1, refines the caching approach from ADR-0002

**Context:** The current cache (ADR-0002) is keyed by the exact
`(symbol, interval, start, end)` tuple as a CSV filename. Any change to
the requested range -- even asking for one additional day -- misses
the cache entirely and re-fetches the *whole* range from the provider,
even though most of it was already downloaded under a different key.
That's not how a system that's actually trying to minimize vendor
calls should behave, especially once daily/scheduled runs start asking
for "yesterday's new candles" on top of a year of existing history.

**Decision:** Change the cache to be keyed per `(symbol, interval)`
rather than per exact range, storing the widest span fetched so far.
On each request: load the existing cache for that `(symbol, interval)`
if present, determine which sub-range(s) of the requested
`[start, end]` are NOT already covered, fetch only those missing
candles from the provider, merge them into the existing cached data
(concatenate, de-duplicate on timestamp keeping the newest), run the
ADR-0006 validation layer on the merged result, save the merged and
validated dataset back to the `(symbol, interval)` cache file, and
return just the requested `[start, end]` slice to the caller.

**Consequences:** Large win for repeated/incremental usage -- a daily
job only pays for the new candles, not the whole history again.
Requires reworking the cache key scheme and adding gap-detection logic
(which contiguous sub-ranges are actually missing, not just "hit or
miss"). Should land together with or after ADR-0006, since merged data
needs to pass validation before it's trusted and written back to disk.
This supersedes the *mechanics* of ADR-0002's caching (still correct
about caching living in the service layer, not the provider) without
changing its underlying rationale.

---

## ADR-0008: Split caching into a reusable `CacheManager`, out of `MarketDataService`

**Status:** Accepted and implemented -- Sprint 1

**Context:** `MarketDataService` (ADR-0002) originally did its own
cache-key construction and file I/O directly (`_cache_path`,
`_write_cache`, a raw `pd.read_csv`/`to_csv` pair) living inside
`src/data/service.py`. That was fine while market data was the only
thing being cached, but the roadmap already calls for caching other
kinds of fetched data -- news, options chains, VIX, macro data,
earnings, forex (see `ARCHITECTURE.md`'s capability list) -- and none
of those should have to reimplement "read a DataFrame from disk if
present, otherwise write one" from scratch.

**Decision:** Extract the cache read/write logic into a standalone
`CacheManager` class in `src/utils/cache.py` (not `src/data/`, since
it's explicitly not data-specific). `CacheManager` knows only how to
`get(key) -> DataFrame | None` and `set(key, data)` against a directory
of CSV files -- it has no concept of a symbol, interval, or date range.
`MarketDataService` now owns *only* the domain logic: building a cache
key from a request (`_cache_key`), deciding whether to consult the
cache, and what to fetch from the `DataProvider` on a miss. It holds a
`CacheManager` instance (constructed from `cache_dir`, or injected
directly via a new `cache` constructor argument) instead of touching
files itself. The pipeline is now:

```
MarketDataService  -- domain logic (what to fetch, when to cache)
        |
        v
CacheManager        -- generic key -> DataFrame persistence
        |
        v
DataProvider         -- vendor-specific fetching (unchanged, ADR-0002)
```

**Consequences:** Any future capability that fetches from an external
source can reuse `CacheManager` directly instead of rebuilding file
I/O -- the reason this was worth doing now rather than after a second
or third caching capability had already reinvented it independently.
`MarketDataService`'s public API is unchanged (`get_candles`,
`get_history`, same constructor signature plus one new optional `cache`
argument), so this was a pure internal refactor: all 15 existing
`MarketDataService` tests pass unmodified, plus 6 new tests directly
against `CacheManager` in `tests/test_cache.py` that don't reference
market data at all. Total suite: 28 tests (7 config + 15 market data +
6 cache).

---

## ADR-0009: Sprint 2 builds a research engine before strategies

**Status:** Accepted -- Sprint 2

**Context:** `ROADMAP.md` originally scoped Sprint 2 as narrowly
"build indicators" with strategies following in Sprint 3. The
alternative proposed instead: use Sprint 2 to build a reusable
quantitative research framework -- an Indicator Engine (the only place
indicators are calculated), rule-based Market Regime Detection, a
Backtesting Framework (run strategy -> collect trades -> calculate
metrics -> generate report), and an Experiment Registry that turns
every backtest into a permanent, comparable record. Strategies
themselves are deliberately not part of this sprint.

**Decision:** Adopt the broader scope. Reasoning: once this
infrastructure exists, adding a new strategy means writing signal logic
that calls the Indicator Engine and gets evaluated by the existing
backtesting framework -- a matter of hours, with results automatically
comparable to every prior experiment. Without it, each new strategy
would likely reinvent its own ad hoc indicator calculations and its own
one-off backtest script, with no structured way to compare results
across strategies or track which changes actually helped. This is the
same reasoning as ADR-0001 (capability-based organization) and ADR-0002
(the `DataProvider` abstraction) applied one layer up: build the
reusable seam before building the things that plug into it.

**Consequences:** Sprint 3 (Strategies) is now blocked on Sprint 2
landing all four modules, rather than following immediately after a
narrower indicators-only Sprint 2. The Backtesting Framework (Module 3)
will need to be validated against a minimal/dummy strategy interface
before any real strategy exists, since `src/strategies/` remains an
empty package until Sprint 3. The Experiment Registry (Module 4) is a
genuinely new concept with no existing precedent in this codebase to
extend -- its storage format and query interface are open design
questions to be resolved when that module is built, not assumed here.

---

## ADR-0010: Regime scoring is continuous, exposes all six regimes from day one

**Status:** Accepted -- Sprint 2

**Context:** Market Regime Detection (Module 2) needs to classify
Trending/Ranging, Volatile/Low Volatility, and Risk-On/Risk-Off. Only
the first two axes have a real, computable signal today (from moving
averages and ATR, via the Indicator Engine); risk-on/risk-off needs a
market-wide proxy (e.g. VIX, credit spreads) that doesn't exist as a
data source yet.

**Decision:** `MarketRegimeEngine.score()` returns a DataFrame with all
six regime names as columns from the start -- `risk_on`/`risk_off`
simply return `NaN` for every row until a real signal exists to back
them. Each axis is a continuous score in `[0, 1]` (e.g. `trending: 0.82,
ranging: 0.18`), not a single binary label, computed as a saturating
function of indicator separation (trend) and a rolling percentile rank
(volatility). `dominant()` collapses each axis to a label
(`"trending"`, `"low_volatility"`, `"unknown"` for the risk axis) when
a single classification is actually needed, e.g. for a report.

**Consequences:** A strategy or report written against this engine's
output today already has `risk_on`/`risk_off` columns to read -- when
a VIX-based (or similar) signal is added later, it's a change inside
`_risk_score()` (a method that doesn't exist yet), not a change to
every caller's code. Continuous scores cost a little more to compute
and reason about than plain labels, but let two regimes coexist
meaningfully (a market can be both trending and volatile at once) and
avoid hard cutoffs at arbitrary thresholds.

---

## ADR-0011: Strategy interface and Backtester execution model

**Status:** Accepted -- Sprint 2

**Context:** The Backtesting Framework (Module 3) needs a strategy
interface to run against, but no real strategy exists yet (that's
Sprint 3). Two things needed to be settled before the framework could
be written at all: what shape a strategy takes, and what execution
model the backtester simulates (position sizing, fills, costs).

**Decision:** `Strategy` is a `typing.Protocol` (structural typing, no
required base class) in `src/strategies/base.py` with three members:
a `name` property, `prepare(data) -> DataFrame` (enrichment -- adding
indicator/regime columns, typically via `IndicatorEngine`/
`MarketRegimeEngine`, never computing them inline), and
`generate_signals(data) -> DataFrame` (must include a `signal` column
valued -1/0/1; additional columns are allowed and ignored today). The
Backtester (`src/backtesting/`) runs the minimal simplified execution
model: one unit of position size per signal, entries/exits at the
candle's close, no transaction costs or slippage, and the position is
shifted forward one bar before being applied to returns so a signal
computed from bar t's close can't act on bar t's own move (no
lookahead). `Trade`, `BacktestResult`, and `calculate_metrics()`
(Sharpe, win rate, total return, max drawdown) are plain, independently
testable functions/dataclasses.

**Consequences:** Any object with the right three members satisfies
`Strategy` -- Sprint 3's real strategies don't need to inherit from
anything, just implement the shape. The simplified execution model
(no costs, no slippage, single unit size) means backtest results from
this framework are directionally useful, not yet realistic for sizing
or cost-sensitive decisions -- that's explicitly deferred to
`src/risk/` and `src/execution/` (Sprint 4), not solved here. Verified
against `ScriptedStrategy` (a test double with a hand-specified signal
series, for exact trade/equity-curve verification) and a minimal real
`SmaCrossStrategy` that calls `IndicatorEngine` from `prepare()`,
demonstrating the intended usage pattern end-to-end.

---

## ADR-0012: Experiment Registry backed by SQLite

**Status:** Accepted -- Sprint 2

**Context:** The Experiment Registry (Module 4) needs to store
potentially hundreds of experiment records over the project's life and
support querying them (e.g. "every KEEP decision for this strategy").
Two options were considered: one file (JSON or YAML) per experiment
plus a CSV index for scanning, or a single SQLite database.

**Decision:** Use SQLite (`src/experiments/registry.py`, stdlib
`sqlite3`, no new dependency). One `experiments` table; `changed`,
`metrics_before`, and `metrics_after` are stored as JSON-serialized
text columns (via the stdlib `json` module) rather than a fixed
per-metric schema, since what can change (an indicator parameter today,
a whole strategy swap tomorrow) and which metrics get tracked will both
evolve. `ExperimentRegistry` is deliberately decoupled from
`src/backtesting` -- it stores whatever dicts it's given and has no
import dependency on `BacktestResult` or `Trade`; the caller converts
two `BacktestResult.metrics` dicts into `metrics_before`/`metrics_after`.

**Consequences:** Filtering and counting (`list_experiments(decision=...,
strategy_name=...)`, `count()`) are real SQL queries, not full-file
scans, so this stays fast as the table grows into the hundreds of rows
the whole feature is designed around. The cost relative to the
file-per-experiment alternative: the database isn't human-diffable in
a `git diff` the way a JSON file would be, and inspecting an experiment
outside Python means opening it with a SQLite client rather than just
reading a file. `Experiment.summary()` exists specifically to give a
human-readable view without needing that.

---

## ADR-0013: `atp doctor` -- a registry-based system health check

**Status:** Accepted -- pre-Sprint 3

**Context:** Before starting Sprint 3, the project wanted a single
command that answers "is everything actually working?" without having
to guess which subsystem broke -- Python version, config, the live data
provider, the on-disk cache, the experiments database, and (eventually)
broker connectivity and API keys. Two of those last two don't exist
yet: `src/broker/` is Sprint 5 scope, and the only data provider today
(`yfinance`) needs no API key. A generic "Database" check was also
proposed alongside "Experiments DB," but the project has exactly one
database -- there's nothing separate to check.

**Decision:** Build `src/cli/`, following the same registry pattern as
`src/indicators/registry.py` (ADR-0000's extensibility test applied
directly): each health check is a plain no-argument function returning
a `CheckResult` (`src/cli/registry.py`), registered with
`@register_check("Display Name")` in `src/cli/checks.py`. `atp doctor`
(`src/cli/doctor.py`) runs every registered check and prints one line
per check. Checks for capabilities that don't exist yet (Broker
Connection, API Keys) report a third status, `NOT_IMPLEMENTED`, rather
than being silently omitted or faked as passing -- the output is
supposed to be an honest, complete list of everything the system will
eventually need to be healthy. The proposed "Database" check was folded
into "Experiments DB" since they'd check the identical thing.
Implemented checks (Python Version, Configuration, Market Data, Cache,
Experiments DB) hit the real filesystem/provider/database, not mocks --
`Market Data` deliberately bypasses the cache (`use_cache=False`) so a
cache hit can't mask a provider outage. Invoked today as `python -m
src.cli doctor`, not a bare `atp` command; wiring a real global `atp`
requires the console-script packaging tracked in ADR-0004, which is
deliberately still deferred to pre-1.0.

**Consequences:** Adding a check for Broker Connection or API Keys once
those capabilities exist in Sprint 5 means writing one function and
decorating it -- `doctor.py` doesn't change. A failing check reports
which subsystem broke and why (e.g. "Market Data ✗ (vendor
unreachable)") instead of a stack trace three layers deep in whatever
was using it. `atp doctor` exits 1 if anything failed and 0 otherwise
(NOT_IMPLEMENTED checks don't block a healthy exit code), so it's
scriptable in CI once ADR's "no CI yet" gap (tracked in `ROADMAP.md`)
is closed. Cost: this is one more thing to keep in sync -- a new
capability that should be health-checkable (e.g. a second data
provider) needs its own check written deliberately, it doesn't happen
automatically.

---

## ADR-0014: Extension Cost as a standing awareness metric

**Status:** Accepted -- pre-Sprint 3

**Context:** ADR-0000 states extensibility as the project's north star
-- "does this make future extensions easier or harder?" -- but that
test is a judgment call with nothing concrete behind it. Without some
habit of measurement, "the architecture stays extensible" is easy to
believe and hard to verify, especially six months from now when it's
tempting to just make the next addition work however is fastest.

**Decision:** Adopt **Extension Cost** -- the count of *existing* files
modified (not created) to add one new indicator, strategy, broker, or
data vendor -- as a permanent habit, not a pass/fail gate. There is no
target number to hit per capability, and no threshold that makes a
given addition "fail." The point is staying aware of the number every
time, and using judgment on what it implies: a couple of existing files
touched to wire in something new is normal and expected; needing to
touch a large fraction of the codebase is the actual signal --
concretely, if adding one feature means editing something like half the
project's files, that's a sign the architecture has been violated
somewhere, whether or not any single number was "supposed" to be zero.

Process: the `CHANGELOG.md` entry for a new indicator, strategy,
broker, or data vendor includes a line -- `Extension Cost: N file(s)
changed: <list>`. If that number looks disproportionate for what was
added, it gets flagged and discussed in the same entry (why, and
whether it's one-time or a recurring pattern) rather than absorbed
silently -- the same discipline already applied to architectural
conflicts (ADR-0003). A new tradable *symbol* (e.g. adding "TSLA" to
the watchlist) isn't tracked under this metric -- that's a config data
change, not a code extension.

**Consequences:** Keeps ADR-0000 grounded in an honest, running record
instead of relying on memory or a vague sense that things are fine,
without pretending a single integer can fully capture "extensible."
Applying it once already surfaced something worth knowing, not fixing:
today, using a second data vendor means changing whatever call site
constructs `MarketDataService(provider=...)` (e.g. `src/main.py`) --
a small, one-file cost, and a reasonable one given there's no
provider-selection factory yet. That's exactly the kind of observation
this metric is for -- noticing it, not necessarily reacting to it. If a
provider-selection factory is ever built (it would naturally fit
alongside ADR-0005's typed `Settings` object, already deferred to
pre-1.0), that cost could drop further, but nothing here requires that
work to happen. No capability has a measured Extension Cost yet, since
nothing has been added incrementally to these seams so far -- the
first real strategy in Sprint 3 will be the first live data point.

---

## ADR-0015: The Signal Framework -- supersedes ADR-0011

**Status:** Accepted -- Sprint 3

**Context:** Sprint 3's theme is "The Research Layer": given historical
data, what opportunity exists, how confident are we, and what evidence
supports it. ADR-0011 (Sprint 2) had strategies return a dense
DataFrame with a `signal` column (-1/0/1) so the Backtesting Framework
had something concrete to run against before any real strategy existed.
That was always a placeholder pending a real contract -- Sprint 3 is
that contract. Three things needed settling: what a `Signal` actually
contains, whether a strategy emits one per candle or only at decision
points, and whether it carries a price.

**Decision:** `Signal` (`src/signals/models.py`) is a frozen dataclass:
`timestamp`, `direction` (`SignalDirection`: `LONG` / `SHORT` / `FLAT`),
`confidence` (0.0-1.0, validated), `metadata` (free-form dict), and
`id` (a `UUID`, assigned client-side at construction via `uuid4()`, not
by a database on insert -- see ADR-0016 for why that matters). No
`price` field: a Signal answers "what position should the portfolio
move toward," not "at what price" -- that's execution's job
(`src/execution`, not built yet), and folding it in would smuggle
order-level thinking back into what's supposed to be a pure decision.
Strategies emit signals sparsely -- one per decision point (e.g. the
moment EMA20 crosses EMA50), not one per candle -- since metadata like
`"reason": "EMA20 crossed EMA50"` is only true at the moment it
happens; repeating it on every unchanged bar would be misleading.
`Strategy.generate_signals(data) -> list[Signal]` replaces the old
DataFrame/`SIGNAL_COLUMN` contract entirely. The Backtester
(`src/backtesting/engine.py`) holds each signal's direction from its
own bar forward until the next signal supersedes it (unshifted
position), then applies the existing ADR-0011 no-lookahead shift once,
in `_compute_equity_curve`, exactly as before -- only the *source* of
the position series changed, from a dense column to a sparse signal
list. A `Trade` now falls directly out of two consecutive signals (the
one that opened it, the one that changed or flattened it) rather than
being inferred by diffing a dense column.

**Consequences:** `src/strategies/base.py`, `src/backtesting/engine.py`,
`src/backtesting/models.py`, and `tests/test_backtesting.py` all
changed as part of this ADR -- this is a supersession of Sprint 2 work,
not a pure addition, and was flagged as such before implementation
started. `SIGNAL_COLUMN` and the -1/0/1 DataFrame convention no longer
exist. Every future strategy (Sprint 3's first real one, and everything
after) speaks this contract from day one -- the Extension Cost
(ADR-0014) of adding a new strategy should now be close to 0 existing
files, same as before, but the shape of what gets added has changed
from "a DataFrame column" to "a list of typed decisions with evidence
attached," which is what Performance Attribution and the AI Research
Reporter (later Sprint 3 modules) actually need to do their jobs.

---

## ADR-0016: Signals are stored as first-class rows in the Experiment Registry

**Status:** Accepted -- Sprint 3

**Context:** ADR-0015 introduced `Signal.id` (a `UUID`) so a `Trade`
can reference the signals that opened and closed it
(`entry_signal_id` / `exit_signal_id`) without embedding the full
`Signal` object -- a `Trade` stays small and storable, and whoever
needs the full evidence behind a trade (Performance Attribution, the
AI Research Reporter) looks it up separately. That raised the question
ADR-0012 (Experiment Registry) didn't need to answer at the time:
where do the actual `Signal` objects live? Two options were
considered: a new, dedicated `SignalRepository` (mirroring why
`CacheManager` was split out of `MarketDataService`, ADR-0008), or
storing signals directly in the existing `ExperimentRegistry`.

**Decision:** Store signals as first-class rows inside
`ExperimentRegistry` (`src/experiments/registry.py`), not a separate
repository. A new `signals` table (`id`, `experiment_id`, `timestamp`,
`direction`, `confidence`, `metadata`), with `save_signals(experiment_id,
signals)`, `get_signals(experiment_id)`, and `get_signal(signal_id)`.
Deliberately added as new methods rather than a new parameter on
`log_experiment()`, so that method's existing signature -- and every
test written against it -- is untouched. This narrows ADR-0012's
"does not know about `BacktestResult`/`Trade`" boundary slightly:
`ExperimentRegistry` now imports `src.signals.models.Signal`, though
still nothing from `src.backtesting`. Signal is treated as a
lower-level, foundational concept (Sprint 3's Module 1) that experiments
(a higher layer) can depend on, the same way `src/backtesting` already
depends on `src/strategies`.

**Consequences:** One SQLite file, one registry class, no new module
to build, test, and maintain -- lower Extension Cost (ADR-0014) today.
The tradeoff, made deliberately rather than by default: signal-keeping
and experiment-keeping are now coupled in one class with two
responsibilities, and if a future capability (live paper trading, a
dashboard) wants to read signal history independently of any specific
experiment, it goes through `ExperimentRegistry` to do it. If that
coupling becomes a real cost later, a `SignalRepository` extraction
would follow the exact precedent ADR-0008 already set -- this ADR
doesn't foreclose that, it just says now isn't that time.

---

## ADR-0017: Every component produces knowledge for the next component

**Status:** Accepted -- Sprint 3

**Context:** Sprint 3 ("The Research Layer") stacks four new modules
-- Signal Framework, Strategy SDK, Performance Attribution, AI Research
Reporter -- on top of Sprint 2's research engine. With that many
layers, it's easy for a shortcut in one layer (e.g. a strategy peeking
at execution details, or attribution reaching back into raw indicator
math) to quietly recreate the tight coupling ADR-0001's capability-based
organization was meant to prevent in the first place. ADR-0000 already
established extensibility as the north star for *adding* new instances
of a capability; this is the complementary principle for what each
*layer* is allowed to hand to the next one.

**Decision:** Adopt as a second standing engineering principle: every
component produces knowledge for the next component, not a finished
decision on the next component's behalf. Concretely, in this
architecture: Market Data doesn't produce trades, it produces clean
data. Indicators don't produce profits, they produce features. Regime
detection doesn't produce trades, it produces context. Strategies don't
produce orders, they produce signals (this is exactly why ADR-0015 gave
`Signal` no `price` field). Backtesting doesn't produce trades as an
end in themselves, it produces evidence -- metrics, attribution, a
record of what happened. Experiments don't produce conclusions, they
produce a permanent, queryable record of what was tried. The AI
Research Reporter (Sprint 3, Module 4) doesn't produce trading
decisions, it produces a research recommendation for a person to weigh.
When designing any new module, the test is: does this component's
output stay knowledge the next layer can use on its own terms, or does
it sneak in a decision that belongs to a layer further downstream?

**Consequences:** Each layer stays independently testable and
independently replaceable -- a strategy can be swapped, a backtester's
execution model can be made more realistic, an attribution method can
change, all without the other layers needing to know. Cost: this
sometimes means a layer produces something less immediately "useful"
on its own (a `Signal` with no price can't be handed straight to a
broker) in exchange for staying honest about which layer actually owns
that decision. Alongside ADR-0000 (extensibility), this is now the
second standing test applied when a design decision is unclear.

---

## ADR-0018: Strategy SDK -- helpers only, never decision-making

**Status:** Accepted -- Sprint 3

**Context:** Sprint 3 Module 2 asked for a Strategy SDK so writing a
strategy stops meaning re-deriving the same boilerplate every time
(wiring up an `IndicatorEngine`, validating input columns, tagging log
lines, remembering `Signal`'s exact fields) -- visible already in how
`SmaCrossStrategy` (`tests/test_backtesting.py`) had to do all of this
by hand. Two designs were considered: an opinionated base class that
owns the `generate_signals` loop and asks the author for a single
vectorized per-bar decision function, or a lighter base class that
still requires the author to implement `prepare()`/`generate_signals()`
themselves, with ready-made helpers for the mechanical parts. The
opinionated option was explicitly rejected: a strategy author should
never be reduced to writing a single method, and the SDK should never
hide the actual decision-making, including the decision of when (and
how often) to emit a signal at all.

**Decision:** Add `BaseStrategy` (`src/strategies/sdk.py`, a plain
`ABC`) as an optional convenience strategies may subclass.
`Strategy` (`src/strategies/base.py`) is unchanged -- still a
`Protocol`, satisfied structurally (ADR-0011); `BaseStrategy` is one
way to satisfy it, not a requirement. `BaseStrategy` provides: `name`
(set once via `__init__`) and `self.log` (a `loguru` logger bound with
the strategy's name); `self.indicator(data, name, **params)` (a thin,
stateless wrapper over `IndicatorEngine`, safe to call across multiple
backtest runs on the same instance since it holds no state between
calls); `self.require_columns(data, *columns)` (validates expected
columns are present -- defaults to the standard OHLCV set -- raising a
clear, strategy-attributed `ValueError`); and `self.emit_signal(timestamp,
direction, confidence, **metadata)` (constructs and logs a `Signal`,
merging in `{"strategy": self.name}` -- author-supplied keys win on
collision). `prepare()` and `generate_signals()` remain abstract: the
author writes both, in full, including when and how often to call
`emit_signal()`.

**Consequences:** A strategy built on `BaseStrategy` (e.g.
`EMACrossStrategy`) still owns 100% of the trading logic and its
expression -- loop, vectorized, whatever the author chooses -- the SDK
only removes setup/plumbing boilerplate, never a judgment call.
Extension Cost (ADR-0014) of a new SDK-based strategy: 0 existing
files (a new file subclassing `BaseStrategy`; `src/strategies/base.py`
and the Backtester are untouched). If a more opinionated, lower-
boilerplate path is ever wanted (e.g. a purely vectorized strategy
style), that would be a separate, distinct class -- not a retrofit of
`BaseStrategy` -- since this ADR deliberately chose not to build that
here.
