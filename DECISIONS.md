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

---

## ADR-0019: Performance Attribution -- regime-at-entry, session breakdown deferred

**Status:** Accepted -- Sprint 3

**Context:** Sprint 3 Module 3 asked backtests to explain results, not
just report win rate: trade counts, average hold time, which regime
trades did best/worst in, and eventually a session-of-day breakdown
(morning/lunch/power hour). Three design questions needed settling
first. (1) Where does regime information come from -- trusting a
strategy to have tagged `Signal.metadata` with regime context, or
recomputing it independently via `MarketRegimeEngine`? Relying on
strategy-supplied metadata would make attribution only work for
strategies that remembered to tag it -- fragile and inconsistent. (2)
A trade spans a range of time (`entry_time` to `exit_time`) -- which
point in that range determines its regime bucket for "Best"/"Worst
Regime"? (3) Session buckets only mean something if candle timestamps
are reliably in market-local time, which `ADR-0006` (timezone
consistency) hasn't landed -- ship the session breakdown on an
unverified assumption, or hold it off?

**Decision:** Add `src/attribution/` (`PerformanceAttributor`,
`AttributionReport`, `RegimeStats`), touching zero existing files.
`PerformanceAttributor.run(result, candles, **regime_kwargs)`
independently recomputes regime via `MarketRegimeEngine(candles)` --
never reads `Signal.metadata` for this -- so attribution works
identically for every backtest regardless of what a strategy chose to
record. Each trade is bucketed by the regime **at its `entry_time`**
(not the dominant regime across its whole hold) -- simplest, matches
"what conditions was this decision made in," and avoids the added
complexity of a trade spanning multiple regimes. Buckets join only the
trend and volatility axes (e.g. "Trending + Low Volatility") --
`risk_regime` is excluded since it's always `"unknown"` until ADR-0010's
VIX gap closes, which would otherwise put every trade in a useless
"+ Unknown" bucket. A trade entering during the regime engine's
indicator warmup period (trend/volatility scores still `NaN`) is
bucketed as `"unknown"` and excluded from "Best"/"Worst Regime" --
`MarketRegimeEngine.dominant()`'s own `NaN >= NaN -> False` comparison
would otherwise silently mislabel it as "ranging"/"low_volatility"
rather than admitting it doesn't know yet. Session-of-day attribution
(morning/lunch/power hour) is **not** included in this module --
deferred until ADR-0006 timezone consistency actually lands, rather
than shipped on an assumption about candle timestamps that the
codebase doesn't yet guarantee.

**Consequences:** Extension Cost (ADR-0014) for this module: 0 --
`src/backtesting`, `src/regime`, and `src/signals` are all untouched;
`src/attribution` only reads their existing public APIs
(`Trade.entry_time`/`return_pct`, `MarketRegimeEngine.score()`/
`dominant()`). `AttributionReport` is in-memory only for now, not
persisted -- wiring it into `ExperimentRegistry` (e.g. a
`save_attribution()` alongside `save_signals()`) is a natural future
step, not built here since it wasn't asked for this round. Session
attribution remains a known gap in `ROADMAP.md`/`PROJECT_STATE.md`
until ADR-0006 lands -- deliberately incomplete rather than silently
wrong.

---

## ADR-0020: AI Research Reporter -- hybrid deterministic findings + optional LLM prose

**Status:** Accepted -- Sprint 3

**Context:** Sprint 3 Module 4 asked for a research report generated
from a completed experiment (strategy, Sharpe, drawdown, trades) --
evidence-based, not generic praise. Two questions needed settling
first. (1) Mechanism: should the report be produced entirely by an LLM
reasoning freely over the numbers, or should the numbers be extracted
deterministically first and an LLM (if used at all) only asked to
rephrase them? A free-reasoning LLM risks stating a number wrong or
inferring a pattern the data doesn't actually support -- exactly the
"generic praise" failure mode this module exists to avoid. (2) Evidence
scope: should the reporter reach for plausible-sounding narrative
(e.g. "losses cluster in the first 20 minutes after open") even though
session-of-day attribution doesn't exist yet (deferred by ADR-0019
pending ADR-0006), or stay strictly within what `PerformanceAttributor`
and `BacktestResult` actually contain today?

**Decision:** Both questions were settled toward strict evidence
grounding. Added `src/research/` as two deliberately separate stages:
`compile_findings(result, attribution)` (`compiler.py`) is a fully
deterministic function that extracts a `ResearchFindings` -- a flat list
of `Finding(label, value)` pairs plus an optional single
`recommendation` string -- from a `BacktestResult` and an
`AttributionReport`. It reasons only about evidence the platform already
has (trade count, win rate, Sharpe, max drawdown, average hold, best/
worst regime) and never claims anything about session-of-day timing,
since that axis doesn't exist yet. A `recommendation` (e.g. "investigate
excluding trades entered during Ranging + Volatile") is only produced
when the worst regime bucket actually has a negative average return --
otherwise `None`, rather than manufacturing a suggestion. `renderers.py`
then turns `ResearchFindings` into prose via a `NarrativeRenderer`
protocol: `FallbackNarrativeRenderer` (template-based, always available,
zero setup) and `ClaudeNarrativeRenderer` (calls the Claude API with a
system prompt that explicitly forbids stating any number or claim not
already present in the findings, restricts it to rephrasing). Both
renderers get the exact same `ResearchFindings` -- the LLM path can only
ever change how the facts are said, never what facts exist.
`ResearchReporter.run(result, attribution)` picks
`ClaudeNarrativeRenderer` when `ANTHROPIC_API_KEY` is set and
`anthropic` is importable, `FallbackNarrativeRenderer` otherwise, and
falls back to the deterministic renderer on any exception from the LLM
path (missing package, network error, bad key) rather than losing the
report. `anthropic` is a lazy, optional import -- deliberately not added
to `requirements.txt` -- so the platform has zero new required
dependencies. Placed at `src/research/`, distinct from the Sprint 7+
`src/ai/` scope (ML/LLM-based *signal generation*, consumed by
strategies) -- this module produces a recommendation for a person to
weigh (ADR-0017), never a signal or a trading decision. `atp doctor`'s
`check_api_keys` (`src/cli/checks.py`) was upgraded from
`NOT_IMPLEMENTED` to a real, always-`OK` check reporting whether
`ANTHROPIC_API_KEY` is set -- its absence doesn't degrade platform
health, since the fallback renderer always works. `format_timedelta`
was promoted from a private helper in `src/attribution/models.py` into
`src/utils/formatting.py` so both `AttributionReport.report()` and
`compile_findings()` share one implementation, following the same
extraction-on-second-use precedent as `CacheManager` (ADR-0008).

**Consequences:** Extension Cost (ADR-0014) for this module: 4 files
changed outside the new `src/research/` package --
`src/attribution/models.py` (import + call site swapped to the shared
helper, private function removed), `src/utils/__init__.py` (export
added), `src/utils/formatting.py` (new file, not a change to an
existing one), and `src/cli/checks.py` (`check_api_keys` rewritten).
Not purely additive like Module 3, but proportionate: three of the four
touches are a single import/export line each, and the fourth
(`check_api_keys`) is a rewrite of one existing function's body, not a
change to its signature or callers. A completed experiment's
`ResearchReport` is not yet persisted back into `ExperimentRegistry` --
it's produced on demand from a `BacktestResult` + `AttributionReport`
pair, the same "in-memory for now" posture ADR-0019 took with
`AttributionReport`; wiring it into the registry is a natural future
step, not built here since it wasn't asked for this round. Sprint 3's
own first strategy (an EMA-cross or opening-range-breakout, per the
sprint's own instruction that complexity should come after confidence
in the platform) still hasn't been built as a permanent
`src/strategies/` file -- `EMACrossStrategy` remains a demonstration
class in `tests/test_strategy_sdk.py` only, tracked in
`PROJECT_STATE.md`/`ROADMAP.md` as the next concrete piece of work.

---

## ADR-0021: Position sizing -- fixed fraction of equity, standalone from Backtester

**Status:** Accepted -- Sprint 4

**Context:** Sprint 4 asked for `src/risk`: position sizing plus
per-trade and portfolio-level exposure limits, given a signal and
account state. Every prior module in this codebase decides one thing
deliberately and defers the rest (Signal has no price; `BaseStrategy`
never picks a direction; `PerformanceAttributor` doesn't touch session
timing) -- three questions needed the same treatment here. (1) Sizing
basis: should position size scale with `Signal.confidence` (giving that
field its first real consumer), or use a fixed fraction of equity
regardless of confidence? (2) Portfolio-level limit: cap total deployed
capital as a percentage of equity, cap the number of concurrent
positions, or both? (3) Should `PositionSizer` wire directly into
`Backtester.run()` now, changing already-shipped, tested code, or stay
a standalone module this round?

**Decision:** All three were settled toward the simpler, more
conservative option, matching the "deliberately simple first" posture
`EMACrossStrategy` set. Added `src/risk/` (`PositionSizer`,
`RiskLimits`, `AccountState`, `SizingDecision`), touching zero existing
files. `RiskLimits.risk_per_trade_pct` (default `0.10`) is a **fixed**
fraction of account equity deployed per trade -- applied identically to
every signal regardless of `Signal.confidence`. Confidence-scaled
sizing is a natural, compatible future extension (a second
`RiskLimits`/sizer variant, not a rewrite), deliberately not built this
round since there's no validated relationship yet between a strategy's
confidence score and how much capital it should actually be trusted
with. `RiskLimits.max_portfolio_exposure_pct` (default `0.50`) caps
total capital committed to open positions as a percentage of equity --
chosen over a raw position-count cap since it answers the more direct
question ("how much of the account is at risk right now") regardless
of how many separate positions that capital happens to be split across;
a count-based cap can be layered on later if it turns out to matter
independently. `PositionSizer.size(signal, account, price)` is
**standalone** this round: `Backtester` keeps ADR-0011's existing
single-unit execution model completely untouched. Wiring position
sizing into a backtest -- or into the future `src/execution` -- is a
deliberate future step, not built here, since doing both in the same
change would make it hard to tell whether a regression came from the
new sizing logic or from the backtest engine it got wired into.
`PositionSizer.size()` raises on a `FLAT` signal (there's nothing to
open a position for) and on a non-positive price; when the full desired
allocation doesn't fit in remaining portfolio headroom, it sizes down
to whatever headroom remains rather than rejecting outright, and only
rejects (`approved=False`, `position_size=0.0`) when there's no
headroom left at all -- a smaller position within limits is treated as
better than discarding a valid signal entirely. `LONG` and `SHORT`
signals are sized identically, since both commit capital, just in
opposite directions.

**Consequences:** Extension Cost (ADR-0014) for this module: 0 --
`src/risk` only reads `Signal`'s existing public fields (`direction`),
nothing in `src/backtesting`, `src/strategies`, or `src/signals` was
changed to add it. `PositionSizer`'s output (`SizingDecision`) isn't
consumed by anything yet -- there is no `src/execution` to hand it to,
and `Backtester` doesn't ask it for a size -- so this module is
verified in isolation, not proven end-to-end against a real backtest,
until `src/execution` exists to close that loop. Confidence-scaled
sizing, whole-share-lot rounding, and a position-count-based portfolio
limit are all explicitly deferred, not rejected -- tracked in
`ROADMAP.md`/`PROJECT_STATE.md` as future variants once real usage
shows they're needed.

---

## ADR-0022: Paper execution -- market orders, instant fills, no mark-to-market

**Status:** Accepted -- Sprint 4

**Context:** Sprint 4 asked for `src/execution`: translate a sized
signal into orders, with paper execution first and real broker
connectivity after. This is the first module positioned to actually
consume `PositionSizer`'s `SizingDecision` (ADR-0021 left that loop
open deliberately). The main question was scope: build only the
translation from a `Signal` + `SizingDecision` into an `Order` object,
or go further and simulate actual fills plus a tracked portfolio
(cash and positions), closing ADR-0021's loop this round. Asked
directly; the answer was to build the fuller version.

**Decision:** Added `src/execution/` (`PaperBroker`, `Order`,
`OrderSide`, `Fill`, `Position`), touching zero existing files.
`PaperBroker.submit_signal(signal, symbol, fill_price,
sizing_decision=None)` is the single entry point: it translates
`signal` into an `Order` (`LONG`/`SHORT` -> `BUY`/`SELL` to open,
requiring an approved `sizing_decision`; `FLAT` -> whichever side
closes the existing position, needing no `sizing_decision` at all,
since `PositionSizer` already refuses to size a `FLAT` signal),
"fills" it instantly and completely at the caller-supplied
`fill_price` (no partial fills, slippage, or commission -- the same
simplifications ADR-0011 made for `Backtester`), and updates internal
cash/position bookkeeping. Only one open position per symbol at a
time is supported -- opening a second position in a symbol that
already has one raises, rather than silently averaging or scaling into
it; that's a deliberately deferred feature, not an oversight. Cash
accounting is uniform regardless of direction: a `BUY` always pays
cash out, a `SELL` always brings cash in, whether that `SELL` is
opening a short or closing a long -- this is what makes both directions
"just work" through the same code path without special-casing shorts.
`PaperBroker.account_state` returns a real `src.risk.AccountState`:
`equity` is cash plus each open position's *signed* value at its own
entry price (`quantity * entry_price`, which is negative for a short,
correctly netting out its liability), and `open_exposure` is the sum
of each position's *unsigned* cost basis (`abs(quantity) *
entry_price`), matching what `RiskLimits.max_portfolio_exposure_pct`
is meant to cap regardless of direction. Positions are **not** marked
to market: an open position's contribution to `equity` stays frozen at
its entry price until it's closed and the resulting P&L is realized
into cash -- this broker has no ongoing price feed of its own (like
`PositionSizer`, every price is supplied by the caller), so there is no
"current price" to mark against between fills.

**Consequences:** Extension Cost (ADR-0014) for this module: 0 --
`src/execution` only reads `Signal`'s and `SizingDecision`'s existing
public fields; nothing in `src/risk`, `src/signals`, `src/backtesting`,
or `src/strategies` was changed. This is the first module verified
against `src/risk` directly (tests construct `SizingDecision`s and feed
them to `PaperBroker`), though still not wired into `Backtester` or any
real strategy loop -- that end-to-end proof (strategy -> signals ->
sizer -> broker, all driven by real candles) is a natural next step,
not built here. No mark-to-market means `AccountState.equity` between
fills can understate or overstate the account's true value whenever an
open position has moved in price -- acceptable for now since nothing
downstream depends on inter-fill equity accuracy yet, but worth
revisiting once `src/execution` needs to answer "what is this account
worth right now," not just "what did it realize so far." Real broker
connectivity, limit orders, partial fills, slippage, commission, and
multi-position-per-symbol averaging are all explicitly deferred, not
rejected -- tracked in `ROADMAP.md`/`PROJECT_STATE.md`.

---

## ADR-0023: Broker connectivity -- Alpaca, connectivity + account state only, paper by default

**Status:** Accepted -- Sprint 5

**Context:** Sprint 5 asked for `src/broker`: the platform's first real
broker/exchange integration, behind an interface analogous to
`DataProvider` so the specific broker stays swappable (`ROADMAP.md`).
Three questions needed settling first. (1) Which broker: Alpaca (a REST
API with no local software and a built-in paper-trading environment)
or Interactive Brokers (broader market access, but requires running
TWS/IB Gateway locally and a heavier API)? (2) Credentials: build
against a real account now, or build and test the integration without
one, adding real credentials later? (3) Scope: should this first pass
also submit real orders, or stop at connectivity and account state?
That last question mattered because `src/execution`'s existing
`Order`/`Fill` model (ADR-0022) assumes a synchronous, instantly-and-
completely-filled order -- true for a paper-trading simulation, but not
how a real broker actually works (an order can sit pending, fill
partially, or be rejected asynchronously).

**Decision:** Alpaca was chosen, matching the platform's "deliberately
simple first" posture elsewhere. The integration is built and tested
now against an injected fake HTTP session -- no real Alpaca account
exists yet -- the same "unit-test the parsing and error-handling logic,
leave the live vendor to a future integration suite" posture
`tests/test_market_data.py` already takes toward `YFinanceProvider`
(there is no `test_yfinance_provider.py` either). Real credentials can
be added later via `ALPACA_API_KEY`/`ALPACA_API_SECRET` environment
variables with zero code changes, the same convention `ANTHROPIC_API_KEY`
already established (ADR-0020). Scope is **connectivity and account
state only**: `BrokerConnection` (`src/broker/base.py`) has exactly one
abstract method, `get_account() -> AccountState` -- reusing
`src.risk.AccountState` directly rather than inventing a parallel
"BrokerAccount" model, since that's the same currency `PositionSizer`
already consumes from `PaperBroker`. Order submission against a real
broker is deliberately deferred to a separate, later round: designing
an order/fill model that honestly represents a real broker's
asynchronous lifecycle (pending, partial, rejected) is a big enough
design surface on its own that it shouldn't be rushed into this pass
just to reuse `src/execution`'s existing synchronous shape where it
doesn't actually fit. `AlpacaBroker` defaults to Alpaca's **paper**
endpoint (`ALPACA_PAPER_BASE_URL`) -- connecting to the live, real-money
endpoint (`ALPACA_LIVE_BASE_URL`) requires an explicit, deliberate
override, never a default, since this platform is a research and
paper-trading tool. Missing or partial credentials raise
`BrokerAuthenticationError` immediately in `__init__`, before any
network call is attempted, matching the eager-validation pattern
`RiskLimits`/`AccountState`/`Signal` already use in their own
`__post_init__`s. `atp doctor`'s "Broker Connection" check
(`DECISIONS.md`, ADR-0013) was upgraded from an unconditional
`NOT_IMPLEMENTED` to a real check gated on configuration: still
`NOT_IMPLEMENTED` when `ALPACA_API_KEY`/`ALPACA_API_SECRET` aren't set
(there's genuinely nothing to connect to), otherwise a live
`OK`/`FAIL` via `AlpacaBroker().get_account()`.

**Consequences:** Extension Cost (ADR-0014) for this module: 1 file
changed outside the new `src/broker/` package -- `src/cli/checks.py`
(`check_broker_connection` rewritten; same shape of change as
ADR-0020's `check_api_keys` upgrade). Nothing in `src/risk`,
`src/execution`, `src/signals`, or `src/backtesting` was touched.
`AlpacaBroker.get_account()` is untested against Alpaca's real API --
only against a fake session -- so the first real network call will
still be the true test of whether Alpaca's actual response shape
matches what `_parse_account()` expects; that's an accepted gap, not
an oversight, consistent with how `YFinanceProvider` has never been
directly unit-tested either. Order submission, Interactive Brokers (or
any second broker), and reconciling `PaperBroker`'s simulated fills
against a real broker's actual fills are all explicitly deferred, not
rejected.

---

## ADR-0024: Order submission -- broker-native models, submit + status only, no cancel

**Status:** Accepted -- Sprint 5

**Context:** ADR-0023 shipped `src/broker` connectivity/account-state
only, deliberately deferring order submission because a real order's
asynchronous lifecycle (pending, partial fill, rejection) doesn't fit
`src/execution`'s synchronous, instant-fill `Order`/`Fill` model
(ADR-0022). Building order submission raised the exact question
ADR-0023 flagged: what shape should a submitted order take, and where
should that shape live? The most direct option was reusing
`src.execution.models.Order`/`OrderSide` directly in `src/broker` --
less code, one `OrderSide` enum instead of two. That was rejected: it
would create a real import dependency running opposite to this
codebase's intended direction. `ARCHITECTURE.md`'s dependency diagram
already draws `execution --> broker` as an aspirational future edge --
broker connectivity is meant to be the more foundational capability a
future execution layer builds on, not the other way around. Importing
`src.execution` from `src.broker` today would lock in the wrong
direction the first time it was convenient, silently, without ever
being decided. This reasoning was arrived at during implementation
(not asked for directly) and is disclosed here per this project's norm
of never making a consequential architectural choice quietly.

**Decision:** Add `src/broker/models.py` with its own `OrderSide`
(`BUY`/`SELL`), `OrderStatus` (`PENDING`/`PARTIALLY_FILLED`/`FILLED`/
`REJECTED`/`CANCELED`), `OrderRequest` (`symbol`, `side`, `quantity` --
market order only, no limit price, matching `PaperBroker`'s own
simplicity; raises `ValueError` on non-positive `quantity`), and
`BrokerOrder` (the broker's own record: `broker_order_id`, `symbol`,
`side`, `quantity`, `status`, `filled_quantity`, `filled_avg_price`) --
all deliberately independent of `src.execution.models`, duplicating the
tiny two-value `OrderSide` enum rather than importing across that
boundary. `BrokerConnection` (`src/broker/base.py`) gains two abstract
methods: `submit_order(request) -> BrokerOrder` and
`get_order(broker_order_id) -> BrokerOrder`. Scope stops at submit +
status check -- **no cancellation** this round. `AlpacaBroker`
implements both against `POST /v2/orders` and `GET /v2/orders/{id}`,
sharing a new `_request()` helper with `get_account()` (network
exceptions and 401/403/non-2xx handling, previously inlined only in
`get_account()`, now consolidated in one place). Alpaca's raw order
`status` strings are mapped to `OrderStatus` via a module-level
`_STATUS_MAP`; an unrecognized status **raises** `BrokerConnectionError`
rather than silently defaulting to some guessed status -- the same
"never fake a pass" posture `atp doctor` (ADR-0013) already takes,
applied here to not fake knowing whether an order filled.

**Consequences:** Extension Cost (ADR-0014) for this addition: 0 files
changed outside `src/broker/` for the models/interface/implementation
themselves -- `base.py` and `alpaca.py` were extended, `models.py` is a
new file, and `__init__.py`'s exports grew, all within the package that
already owns this capability; nothing in `src/execution`, `src/risk`,
or `src/signals` was touched. A future execution layer that actually
wants to route orders through `AlpacaBroker` will need to translate
between `src.execution.models.Order` and `src.broker.models.OrderRequest`
at that boundary -- a small, explicit translation cost, paid once, in
exchange for keeping today's dependency direction honest rather than
backwards. Order cancellation, limit/stop order types, and reconciling
`PaperBroker`'s simulated fills against real Alpaca fills remain
explicitly deferred, not rejected. `submit_order`/`get_order` are
untested against Alpaca's real API -- only against a fake session --
the same accepted gap ADR-0023 already established for `get_account()`.

---

## ADR-0025: Order cancellation -- fire-and-confirm, not fire-and-know

**Status:** Accepted -- Sprint 5

**Context:** ADR-0024 shipped `submit_order`/`get_order` but explicitly
left cancellation out, to keep that round's scope contained. Adding
`cancel_order` raised one real design question: what should it return?
Alpaca's `DELETE /v2/orders/{id}` responds with `204 No Content` on
success -- an empty body, not an order record -- because cancellation
is asynchronous. The broker accepting the cancellation request doesn't
mean the order is actually canceled yet: it may already have filled, or
may fill in the brief window before the cancellation takes effect. Two
options were considered: return `None` from `cancel_order`, requiring a
separate `get_order()` call to learn the actual outcome; or have
`cancel_order` call `get_order()` internally and return the resulting
`BrokerOrder`, giving callers a status in one call. The second option is
more convenient but risks implying a certainty the platform doesn't
have -- the status returned would just be whatever `get_order()` saw a
moment after cancellation, not a guarantee of the final outcome.

**Decision:** `cancel_order(broker_order_id) -> None`. A successful
call confirms only that the broker *accepted* the cancellation request,
matching what Alpaca's `204` response actually tells us -- nothing
more. `AlpacaBroker.cancel_order()` calls `DELETE /v2/orders/{id}`
through the existing `_request()` helper, extended with a
`parse_json=False` option so it can accept a `204` empty-body response
without trying to call `.json()` on it (the `_Session` Protocol gained
a `.delete()` method to match). A caller that wants to know whether the
order actually ended up canceled, partially filled, or filled anyway
calls `get_order()` afterward, using tools that already exist rather
than a new bespoke path. `cancel_order` maps errors the same way
`submit_order`/`get_order` do: 401/403 -> `BrokerAuthenticationError`,
any other non-2xx (including a `422` for an order that's no longer in a
cancelable state, e.g. already filled) -> `BrokerConnectionError`, a
network exception -> `BrokerConnectionError`.

**Consequences:** Extension Cost (ADR-0014) for this addition: 0 files
changed outside `src/broker/` -- `base.py` gained one abstract method,
`alpaca.py`'s `_Session`/`_request()` were extended in place, no new
file, no change to `models.py` (cancellation needed no new data shape).
`BrokerConnection`'s three methods now cover the full lifecycle this
platform commits to supporting: submit, check status, cancel -- nothing
built on top of it needs to guess whether a cancellation "worked" from
a return value that couldn't honestly promise that anyway. Limit/stop
order types, a second broker, and reconciling `PaperBroker`'s simulated
fills against real Alpaca fills remain explicitly deferred, not
rejected. `cancel_order` is untested against Alpaca's real API -- only
against a fake session -- the same accepted gap every other
`AlpacaBroker` method already carries.

---

## ADR-0026: Second broker -- Interactive Brokers, Client Portal Web API, connectivity only

**Status:** Accepted -- Sprint 5

**Context:** `ROADMAP.md` has always listed a second broker as the
real proof that `BrokerConnection` (ADR-0023) is actually swappable,
not just designed to be -- one implementation alone doesn't rule out
that the interface secretly assumes something Alpaca-specific.
Interactive Brokers was the natural choice (broad market access, the
other major retail-friendly API alongside Alpaca), but it required
settling three things Alpaca's integration never had to face. (1)
**Which API surface:** IB exposes a REST-based Client Portal Web API
and a socket-based TWS API (via the `ibapi`/`ib_insync` libraries) --
fundamentally different transport models. (2) **Credentials:** unlike
Alpaca's simple `API_KEY`/`API_SECRET` header pair, an individual/retail
IB account authenticates through IB's Client Portal Gateway -- a local
Java process the user runs and logs into via a browser (username,
password, 2FA) -- so there's no simple credential pair this code could
validate at construction time the way `AlpacaBroker.__init__` does. (3)
**Scope:** IB's real order-placement flow adds two pieces of complexity
Alpaca's doesn't have -- orders reference a numeric contract id
(`conid`) rather than a plain symbol string, and many orders come back
with a "reply" (a risk/suitability warning) that must be explicitly
confirmed via a second call before the order actually places. Building
that properly in the same round as first proving the interface itself
would conflate two different pieces of work.

**Decision:** All three were settled toward the same "prove the seam,
defer the hard part" posture ADR-0023 itself took. **API surface:** the
Client Portal Web API -- REST-based, so `IBKRBroker` (`src/broker/
ibkr.py`) fits the same injectable `_Session` Protocol pattern
`AlpacaBroker` already established, rather than introducing a new
socket-based transport model into the codebase for the TWS API.
**Credentials:** `IBKRBroker.__init__(base_url=IBKR_GATEWAY_BASE_URL,
session=None)` takes no credential arguments at all -- authentication
is the Client Portal Gateway's own already-established browser session,
which this code neither creates nor validates; a call simply raises
`BrokerAuthenticationError` if the gateway reports the session isn't
authenticated (401/403), the same error-mapping shape `AlpacaBroker`
already uses. `IBKR_GATEWAY_BASE_URL` (`https://localhost:5000/v1/api`)
is deliberately not split into a paper/live pair the way
`ALPACA_PAPER_BASE_URL`/`ALPACA_LIVE_BASE_URL` are -- for IB, paper vs.
live is determined by which account was used to log into the gateway,
not by a URL this code chooses, and pretending otherwise would be
dishonest about where that safety boundary actually lives. `get_account()`
resolves which account to query via `GET /iserver/accounts`
(`selectedAccount`, falling back to the first entry in `accounts`) --
required, since every portfolio endpoint is scoped to one account id --
then reads `netliquidation.amount` (equity, required) and
`grosspositionvalue.amount` (open exposure, optional, defaults to
`0.0`) from `GET /portfolio/{accountId}/summary` into an `AccountState`.
**Scope:** `submit_order`/`get_order`/`cancel_order` all raise
`NotImplementedError`, deliberately *not* a `BrokerError` subclass --
this is a known, static gap in what this module supports today, not a
broker-side connectivity or auth failure a caller should retry or
handle the way it would handle `BrokerConnectionError`. `atp doctor`'s
Broker Connection check (`src/cli/checks.py`) is left Alpaca-specific
this round -- extending it to check multiple configured brokers is a
natural future step, not built here since it wasn't the point of this
round.

**Consequences:** Extension Cost (ADR-0014) for this addition: 1 file
changed outside the new `ibkr.py`/`test_ibkr.py` files --
`src/broker/__init__.py` (exports added). `BrokerConnection` is now
proven satisfiable by two independently-implemented brokers with
different transport models, different credential models, and no shared
base class beyond the interface itself and the common
`BrokerError`/`BrokerAuthenticationError`/`BrokerConnectionError`
hierarchy -- exactly the "swap the broker without touching strategies,
risk, or execution" property ADR-0023 designed for, now demonstrated
rather than assumed. `IBKRBroker` cannot actually place, check, or
cancel a real order yet -- a strategy or execution layer that tried to
route live trading through it today would get a clear
`NotImplementedError`, not a silent no-op or a wrong result. IB's order
placement (conid lookup, reply/confirmation handling), session
freshness (the Client Portal Gateway's session needs periodic "tickle"
calls and re-authentication -- not modeled here at all), and
reconciling `IBKRBroker` against `AlpacaBroker`'s behavior for the same
logical operation are all explicitly deferred, not rejected. Like every
other concrete broker in this codebase, `IBKRBroker` is untested
against a real gateway -- only against a fake session.

---

## ADR-0027: Fill reconciliation -- a deliberate exception to the broker/execution independence rule

**Status:** Accepted -- Sprint 5

**Context:** `ROADMAP.md` has always listed reconciling `PaperBroker`'s
simulated fills against a real broker's actual fills as a Sprint 5
item, but it was blocked until order submission (ADR-0024) gave this
codebase a real fill to reconcile against in the first place. With
Alpaca now confirmed working end to end (`atp doctor`'s Broker
Connection check passing against a real paper account) and Interactive
Brokers confirmed inaccessible for this platform's actual use (IB
geo-restricts account access for this deployment, unrelated to
anything this codebase controls), this was the natural next piece of
real, usable value: comparing what `PaperBroker` (`src/execution`,
ADR-0022) assumes -- an instant, complete fill at a caller-supplied
price -- against what a real broker order actually did. That
comparison inherently needs both `src.execution.models.Fill` and
`src.broker.models.BrokerOrder` in the same place, which raised a real
question given ADR-0024's explicit rule that `src/broker` must stay
independent of `src/execution` to keep the dependency direction honest:
does building this violate that rule?

**Decision:** No -- but it's worth stating precisely why, so the
distinction doesn't get lost. ADR-0024's rule is about `src/broker`
itself never importing `src.execution` (or vice versa), so that
neither capability's internals leak into the other and the intended
`execution --> broker` dependency direction stays real rather than
accidentally reversed. A new, separate, higher-level module that
depends on *both* -- without either of them depending on it back -- is
a different shape entirely, and this codebase already has direct
precedent for it: `src/attribution` depends on both `src/backtesting`
and `src/regime` without either depending back (`DECISIONS.md`,
ADR-0019). `src/reconciliation` (new) follows the same shape.
`reconcile_fill(real_order: BrokerOrder, simulated_fill: Fill) ->
FillReconciliation` is a single plain function, no class, matching the
project's established pattern for this kind of comparison
(`calculate_metrics()`, `compile_findings()`). It validates that the
two are actually comparable (same symbol, same side -- compared by
`.value` since `OrderSide` is deliberately two separate enums per
ADR-0024) and that `real_order` has actually filled (`FILLED` or
`PARTIALLY_FILLED`; anything else raises `ValueError`, since there's
nothing real yet to reconcile against). Every price/cost field on
`FillReconciliation` is **side-normalized**: positive always means the
real execution was worse than the simulation assumed, regardless of
`BUY`/`SELL` direction, so a caller never has to re-derive "worse for
which side" from a raw signed difference. Partial fills are handled by
comparing `simulated_quantity` (what `PaperBroker` assumed) against
`real_filled_quantity` (what actually filled) as `quantity_shortfall`,
and `cost_impact` uses the real filled quantity, not the simulated one
-- the dollar impact of a price difference should reflect capital
actually committed, not capital `PaperBroker` merely assumed would be.
Scope this round is a **single-order comparison primitive only** --
aggregating reconciliations across many trades into a summary report
(average slippage, total cost impact across a batch) is a natural
future step, not built here, the same "prove the primitive first"
posture `PositionSizer` (ADR-0021) and `PaperBroker` (ADR-0022) both
took before anything wired them together.

**Consequences:** Extension Cost (ADR-0014) for this addition: 0 files
changed outside the new `src/reconciliation/` package -- it only reads
existing public fields off `Fill`/`Order` (`src.execution.models`) and
`BrokerOrder` (`src.broker.models`), nothing in either package was
touched to support it. This is the first module in the codebase that
deliberately depends on both `src/execution` and `src/broker` at once,
which is fine precisely because it's a comparison/analysis layer, not a
capability either of those two depends on -- the same reasoning that
already justifies `src/attribution`'s dependencies. `reconcile_fill()`
is not wired into any live trading loop -- a caller assembles the
`Fill`/`BrokerOrder` pair by hand today (e.g., after manually
submitting a real Alpaca order and running `PaperBroker` against the
same signal for comparison); a helper that automates capturing both
sides of that comparison from one call is a natural future step, not
built here. Aggregate/batch reconciliation, and reconciling
`IBKRBroker` specifically (blocked today since it doesn't support order
submission at all, per ADR-0026), remain explicitly deferred, not
rejected.

---

## ADR-0028: Third broker -- IG, session-based auth, connectivity only

**Status:** Accepted -- Sprint 5

**Context:** `IBKRBroker` (ADR-0026) proved `BrokerConnection` is
architecturally swappable, but Interactive Brokers itself turned out to
geo-restrict account access for this platform's actual deployment (an
OFAC/Section 311 "special measures" block on IB's end, unrelated to
anything this codebase controls) -- so the interface's swappability had
no real account behind it to exercise. The user has real, working
accounts with two other brokers instead: IG (CFDs, spread betting,
forex) and Tiger Brokers/Tiger Trade (real equities). IG was chosen for
this round -- a plain REST API with no locally running gateway process,
closer to Alpaca's shape operationally than to IB's, and immediately
usable with an account the user can actually reach. Two things needed
settling before building. (1) **Credentials/auth:** unlike Alpaca's
forever-valid static header pair, IG requires an API key *plus* a
username and password, exchanged once via `POST /session` for
short-lived session tokens (`CST`/`X-SECURITY-TOKEN`) that must be
attached to every subsequent request -- a third distinct credential
shape in this codebase (Alpaca: static headers; IB: an already-
established browser session this code doesn't create; IG: a login call
this code does make, producing tokens it must then carry). (2)
**Scope:** IG's real order-placement flow returns a short-lived
`dealReference` from `POST /positions/otc`, confirmed once via
`GET /confirms/{dealReference}` into `ACCEPTED`/`REJECTED` and a
permanent `dealId` -- but there's no ongoing "check order status"
endpoint the way Alpaca/IB have, since a filled market order simply
becomes a position rather than a persistent order object with a
lifecycle. Forcing that shape onto `BrokerOrder`/`OrderStatus` (which
assumes `PENDING`/`PARTIALLY_FILLED`/`FILLED` as things a caller can
poll for) would mean either lying about what `get_order()` can actually
tell a caller, or quietly changing what the interface promises.

**Decision:** Both were settled the same way ADR-0026 settled IB's
equivalent questions. **Credentials:** `IGBroker.__init__(api_key=,
username=, password=, base_url=IG_DEMO_BASE_URL, account_id=None,
session=None)` -- all three credential pieces fall back to
`IG_API_KEY`/`IG_USERNAME`/`IG_PASSWORD` environment variables (the
same convention every other broker/API-key integration in this
codebase already uses), and raise `BrokerAuthenticationError`
immediately if any is missing, before any network call. Login happens
lazily on first use (`_ensure_authenticated()`) and the resulting
`CST`/`X-SECURITY-TOKEN` are cached for the instance's lifetime -- no
session-refresh logic this round, since IG's tokens last hours, not
seconds; a long-lived `IGBroker` instance may eventually need to be
reconstructed to re-authenticate, an accepted gap. `IG_DEMO_BASE_URL`/
`IG_LIVE_BASE_URL` select environment explicitly, the same shape as
Alpaca's pair (unlike IB, where paper vs. live is determined by which
account is logged into the gateway rather than a URL). Which of a
session's accounts to use is resolved via an explicit `account_id`
argument (falling back to `IG_ACCOUNT_ID`), or IG's own "preferred"
account, or the first account returned -- mirroring `IBKRBroker`'s
account-resolution shape exactly. **Scope:** `submit_order`/
`get_order`/`cancel_order` all raise `NotImplementedError`, the same
deliberate choice ADR-0026 made for IB, for the same reason: this is a
known, static gap in what this module supports today, not a broker-side
failure. `get_account()` reads `GET /accounts`, resolves the account,
and maps `balance.balance` (equity, required) and `balance.deposit`
(open exposure, optional, defaults to `0.0`) into an `AccountState` --
`deposit` (margin currently committed to open positions) is a
deliberate, documented approximation of "capital committed," since IG's
CFD/spread-bet products are margined rather than fully paid the way
Alpaca's equities are, and IG's balance summary doesn't expose a
Alpaca-style notional exposure figure directly.

**Consequences:** Extension Cost (ADR-0014) for this addition: 1 file
changed outside the new `ig.py`/`test_ig.py` -- `src/broker/__init__.py`
(exports). `BrokerConnection` is now demonstrated by a third
independent implementation with a third distinct credential/auth model
(static headers, established browser session, and now a login-for-
tokens flow), reinforcing ADR-0026's swappability claim -- and, unlike
`IBKRBroker`, one the platform's actual user can exercise against a
real account. `OrderRequest.symbol` would need to already be an IG
"epic" (e.g. `"CS.D.EURUSD.MINI.IP"`), not a plain ticker, if order
submission is ever built -- the same kind of identifier-mapping caveat
ADR-0026 already flagged for IB's `conid`, noted here now even though
order submission itself is deferred. IG order submission/status,
session token refresh, and reconciling `IGBroker` against `AlpacaBroker`
for the same logical operation are all explicitly deferred, not
rejected. `IGBroker` is untested against IG's real API -- only against
a fake session -- the same accepted gap every other concrete broker in
this codebase carries.

---

## ADR-0029: IG order submission -- resolves synchronously, no polling or cancellation

**Status:** Accepted -- Sprint 5

**Context:** ADR-0028 deferred order submission because IG's real
order-placement flow doesn't fit `BrokerOrder`/`OrderStatus` the way
Alpaca's does: `POST /positions/otc` returns a short-lived
`dealReference`, confirmed once via `GET /confirms/{dealReference}`
into `ACCEPTED`/`REJECTED` and a permanent `dealId` -- but IG has no
endpoint to re-query that `dealId` for status later, because a filled
market order simply becomes a position rather than a persistent order
object with an evolving lifecycle. Two questions needed settling before
building `submit_order()` at all. (1) Given there's no live "check
status later" endpoint, should `get_order()` approximate one anyway
(e.g. via `GET /positions/{dealId}`, checking whether a position still
exists), or stay `NotImplementedError`? (2) IG's order body requires a
`currencyCode` that `OrderRequest` has no field for -- where should
that value come from?

**Decision:** (1) `get_order()` and `cancel_order()` both **stay
`NotImplementedError`**, with an updated message explaining why: a
position-existence check can't reliably distinguish "this position
closed because the order filled and was later closed" from "this deal
was rejected and never became a position" -- approximating a status
here would mean guessing at a distinction IG's API doesn't actually let
this code tell apart, which is worse than admitting the gap plainly.
Cancellation has no meaningful target either, since by the time
`submit_order()` returns, the order has already fully resolved one way
or the other. `submit_order()` instead does the entire submit-and-
confirm round trip itself and returns the **final** `BrokerOrder`
directly -- `FILLED` (with `filled_quantity`/`filled_avg_price` read
from the confirmation's `size`/`level`) when `dealStatus == "ACCEPTED"`,
`REJECTED` when `"REJECTED"` -- so a caller never actually needs to
poll afterward the way they would for Alpaca. An unrecognized
`dealStatus` raises `BrokerConnectionError` rather than guessing,
matching `_parse_order`'s posture in `alpaca.py`. (2) `currencyCode` is
read from the **selected account's own `currency` field** (already
available from the same `GET /accounts` call `get_account()` uses), not
a hardcoded default or a new constructor parameter -- account
resolution was refactored into a shared `_get_selected_account()`
helper so both `get_account()` and `submit_order()` go through the same
one path rather than duplicating the "fetch accounts, pick one" logic.

**Consequences:** Extension Cost (ADR-0014) for this addition: 0 files
changed outside `ig.py`/`test_ig.py` -- entirely contained within the
broker that already owns this capability. `OrderRequest.symbol` must
already be an IG "epic" (e.g. `"CS.D.EURUSD.MINI.IP"`) when calling
`IGBroker.submit_order()`, exactly the caveat ADR-0028 already flagged.
`submit_order()` makes two real network calls per order (place, then
confirm) plus one more to resolve the account's currency if it hasn't
been resolved this call -- three round trips for one order, a real cost
of IG's data model that Alpaca's single-call `POST /v2/orders` doesn't
have. Session-token refresh, IG limit/stop order types, and
reconciling `IGBroker` fills against `PaperBroker` via
`src/reconciliation` all remain explicitly deferred, not rejected.
`submit_order()` is untested against IG's real API -- only against a
fake session -- the same accepted gap every other concrete broker in
this codebase carries.

---

## ADR-0030: Fourth broker -- Tiger Trade, wraps the official `tigeropen` SDK

**Status:** Accepted -- Sprint 5

**Context:** The user's third real, usable account is with Tiger
Brokers -- Tiger Trade (real US/HK/SG equities) and Tiger CFD are both
available; Tiger Trade was chosen for this round as the closer match to
Alpaca's asset class than IG's CFD/spread-bet products. Tiger's auth is
a genuinely new shape in this codebase: every request must be
cryptographically signed with an RSA private key (PKCS#1), not just a
static header (Alpaca), an already-established browser session (IB), or
a login-for-tokens flow (IG). Two questions needed settling before
building anything. (1) **Auth implementation:** hand-roll the RSA
request-signing scheme against raw HTTP, matching every prior broker's
`requests`-based `_Session` Protocol pattern, or wrap Tiger's own
official `tigeropen` Python SDK, which already implements signing
correctly? Hand-rolling carries real risk here -- a subtly wrong
signature is the kind of bug that only surfaces against a real account,
not in a unit test. (2) **Scope:** Tiger's API appears to support a
real order-status and cancellation lifecycle (`get_order`/`get_orders`/
`cancel_order`) more cleanly than IG's does -- should order submission
be built this round, given that?

**Decision:** (1) `TigerBroker` wraps `tigeropen`'s `TradeClient`
rather than hand-rolling signing -- the first broker in this codebase
built on a vendor SDK instead of talking `requests` directly. The
injectable seam is the SDK client object itself (`_TradeClient`
Protocol, requiring only `get_assets(segment, market_value) -> list`),
not an HTTP session -- every test in `tests/test_tiger.py` injects a
fake `_TradeClient` and never imports or requires `tigeropen` to be
installed. `tigeropen` itself is imported lazily, only inside
`_build_client()`, and is deliberately **not** added to
`requirements.txt`, mirroring ADR-0020's precedent for the optional
`anthropic` dependency in `src/research`. Credentials (`tiger_id`,
`private_key_path`, `account`) fall back to `TIGER_ID`/
`TIGER_PRIVATE_KEY_PATH`/`TIGER_ACCOUNT` environment variables, the same
convention every other broker uses, and raise
`BrokerAuthenticationError` immediately if any is missing. There is no
separate paper/live URL -- like `IBKRBroker`, the `account` value itself
determines paper vs. live. A `sandbox_debug` flag defaults to `False`,
since Tiger's own documentation recommends testing against a real paper
account over its sandbox environment. `get_account()` wraps **any**
exception from `get_assets()` into `BrokerConnectionError` -- an
accepted, provisional limitation, since Tiger's own exception taxonomy
wasn't verified in depth this round, so it isn't yet distinguishing
auth failures from connectivity failures the way `_request()` does for
Alpaca/IBKR's HTTP status codes. `equity` maps from
`summary.net_liquidation`; `open_exposure` maps from
`summary.gross_position_value` (defaulting to `0.0` when absent) -- the
most direct account mapping of any broker integrated so far, since
Tiger exposes gross position value as its own field, unlike IG's
margin-based `deposit` approximation. (2) Scope stays **connectivity
and account state only**, the same posture every broker in this
codebase has started with: `submit_order`/`get_order`/`cancel_order`
all raise `NotImplementedError`, even though Tiger's API looks better
suited to a real order lifecycle than IG's -- proving the SDK-wrapper
approach and account-state mapping first is this round's job, not
building order management on top of an unproven foundation.

**Consequences:** Extension Cost (ADR-0014) for this addition: 2 files
changed outside the new `tiger.py`/`test_tiger.py` -- `src/broker/
__init__.py` (exports, docstring). `BrokerConnection` now has a fourth
independent implementation with a fourth distinct credential/auth model
(static headers, established browser session, login-for-tokens, and now
RSA-signed requests via a vendor SDK), and the first proof that this
codebase's broker abstraction survives being backed by someone else's
SDK client rather than a `requests` session it fully controls. The
broad exception-to-`BrokerConnectionError` wrapping in `get_account()`
is a known, documented gap -- a future round that verifies `tigeropen`'s
real exception types could narrow this to distinguish auth failures
from connectivity failures, the same distinction `_request()` already
makes for Alpaca and IBKR. Tiger order submission/status/cancellation,
sandbox-vs-real-paper-account testing, and reconciling `TigerBroker`
fills against `PaperBroker` via `src/reconciliation` are all explicitly
deferred, not rejected. `TigerBroker` is untested against Tiger's real
API or the real `tigeropen` SDK -- only against a fake `_TradeClient` --
the same accepted gap every other concrete broker in this codebase
carries.

---

## ADR-0031: `AccountState` moves to a new, neutral `src/portfolio` package

**Status:** Accepted -- architecture review cleanup, pre-Sprint 6

**Context:** An architecture review of Sprint 4-5's work flagged that
`AccountState` had lived in `src/risk` (`src/risk/models.py`) since
ADR-0021, and every concrete broker in `src/broker` (`AlpacaBroker`,
`IBKRBroker`, `IGBroker`, `TigerBroker`) imported it from there via
`from src.risk.models import AccountState`. That import runs the
dependency arrow backwards: `src/broker` is foundational connectivity
infrastructure -- ADR-0000's capability-based organization treats it as
a layer other things build on, not a consumer of `src/risk` -- while
`src/risk`'s whole job (ADR-0021) is to *consume* account information to
make a sizing decision, not to *define* the account domain model other
layers have to reach into it for. `src/execution`'s `PaperBroker` had
the identical import. This is the same category of problem ADR-0002
already solved once for market data (`DataProvider` living independent
of `MarketDataService`) -- a shared shape should live somewhere nothing
needs to reach backwards to get it.

**Decision:** Create `src/portfolio/` (`models.py` -- `AccountState`,
`__init__.py` -- re-exports it), a capability package that depends on
nothing else in this codebase. `AccountState`'s fields, validation, and
semantics are unchanged -- this is a pure move, not a redesign.
`src/broker/base.py` and all four concrete brokers, `src/risk/engine.py`
and `src/risk/__init__.py`, and `src/execution/engine.py` all now import
`AccountState` from `src.portfolio.models` instead of `src.risk.models`.
`src/risk/models.py` no longer defines `AccountState` at all -- not even
as a re-export -- so a static check of `src/risk/models.py`'s own
source can confirm the class actually moved. `src/risk/__init__.py`
does re-export `AccountState` (`from src.portfolio.models import
AccountState`) purely for import-path convenience (`from src.risk
import AccountState` still works); this is `src/risk` consuming
`src/portfolio`, the correct direction, not the class living in two
places. The resulting dependency shape is `broker -> portfolio`,
`risk -> portfolio`, `execution -> portfolio`, and never `broker ->
risk` -- protected going forward by a static source-inspection test
(`tests/test_architecture.py`) that fails if any `src/broker/*.py` file
re-introduces a `from src.risk` import.

**Consequences:** Extension Cost (ADR-0014) for this cleanup: every
concrete broker file, `src/risk/engine.py`, `src/risk/__init__.py`,
`src/risk/models.py`, and `src/execution/engine.py` each changed one
import line (and, for `src/risk/models.py`, lost the class itself) --
mechanical, not structural, changes; no behavior changed anywhere.
`src/portfolio` is deliberately minimal today (just `AccountState`) --
if the platform later needs richer portfolio/position tracking, that's
a natural place to grow, not a reason to have delayed this move.
`ExperimentRegistry`, `src/backtesting`, and every other module that
never touched `AccountState` are untouched. This ADR is a pure
dependency-direction correction; the four-criteria bar (works / tested
/ documented / extensible) was already met for `AccountState` itself
under ADR-0021, so no new behavior needed new tests -- only the import
paths in every existing test that constructed one changed.

---

## ADR-0032: `RiskLimits.risk_per_trade_pct` renamed to `allocation_per_trade_pct`

**Status:** Accepted -- architecture review cleanup, pre-Sprint 6

**Context:** The same architecture review flagged that
`RiskLimits.risk_per_trade_pct` (ADR-0021) doesn't actually mean "risk"
in the standard trading sense. What it controls is "what fraction of
account equity gets committed to a new position" -- capital allocation.
True risk-per-trade normally means something computed from a maximum
acceptable loss divided by the distance to a stop
(`position_size = max_loss / stop_distance`), so that risk is expressed
in dollars-that-could-be-lost, not dollars-committed. This codebase has
no stop-loss or risk-distance model at all -- `PositionSizer` (ADR-0021)
was deliberately scoped to fixed-fraction-of-equity sizing, with
confidence-scaling and anything stop-based explicitly deferred. Calling
the existing field "risk per trade" implies a loss-based guarantee this
platform does not make: a position sized at `risk_per_trade_pct=0.10`
can lose far more or far less than 10% of equity depending on how far
price moves against it, since nothing here caps the loss directly. Left
unfixed, this is exactly the kind of misleading terminology that
becomes dangerous once real automated trading is on the table -- a
false sense of protection is worse than an honestly-named, more modest
guarantee.

**Decision:** Rename the field to `allocation_per_trade_pct` in
`RiskLimits` (`src/risk/models.py`), `PositionSizer` (`src/risk/engine.py`,
including its `reason` strings, e.g. `"sized at full per-trade risk"` ->
`"sized at full per-trade allocation"`), and every call site and test.
The mathematical behavior is completely unchanged -- still a fixed
fraction of equity, applied identically regardless of
`Signal.confidence`, still respecting `max_portfolio_exposure_pct` the
same way. `RiskLimits`'s docstring now states explicitly, in its own
paragraph, that this setting is capital allocation/exposure, **not**
maximum loss, and that true risk-based sizing (stop distance,
volatility, correlation) is a distinct, unbuilt capability -- not
implemented here specifically because inventing a stop-loss model just
to make the terminology fit would be worse than admitting the gap
honestly (the same posture ADR-0019 took toward session-of-day
attribution: deliberately incomplete rather than silently wrong).
`tests/test_risk.py` gained a test
(`test_risk_limits_is_not_a_maximum_loss_model`) asserting `RiskLimits`
has no stop-distance/max-loss-shaped field, specifically so a future
change can't silently start treating `allocation_per_trade_pct` as a
loss guarantee again without that test forcing a conscious decision.

**Consequences:** Extension Cost (ADR-0014) for this rename: every file
that referenced `risk_per_trade_pct` by name changed (`src/risk/models.py`,
`src/risk/engine.py`, `tests/test_risk.py`,
`tests/test_integration_paper_trading.py`) -- a pure rename, not a
behavior change, so no test's expected numbers changed, only the
keyword argument name and a couple of assertion strings. True
risk-based sizing (stop distance, volatility-scaled sizing, correlation
across open positions) remains explicitly deferred, tracked in
`ROADMAP.md`/`PROJECT_STATE.md`, not designed or started here -- this
ADR only fixes what the *existing* capability is honestly called.

---

## ADR-0033: `Signal` gains a first-class, required `symbol` field

**Status:** Accepted -- architecture review cleanup, pre-Sprint 6

**Context:** `Signal` (ADR-0015) has carried `timestamp`, `direction`,
`confidence`, `metadata`, and `id` since Sprint 3, but never `symbol` --
which instrument a decision was about lived only in whatever surrounding
context happened to carry it (a candle DataFrame implicitly about one
symbol, `PaperBroker.submit_signal`'s own separate `symbol` argument).
This was a reasonable simplification while the platform only ever dealt
with one instrument at a time end to end, but the architecture review
flagged it as no longer safe to leave once `src/broker`, `src/execution`,
and multiple real broker accounts (Alpaca, IG, Tiger) are all in play --
a `Signal` read back in isolation (e.g. via
`ExperimentRegistry.get_signal()`) had no way to say which instrument it
was about, and nothing stopped two signals for different symbols from
being conflated once they left their original DataFrame/backtest
context.

**Decision:** Add `symbol: str` to `Signal` (`src/signals/models.py`) as
a required, non-default, first-class field -- not a `metadata` key, and
not optional -- placed right after `timestamp`. Every producer and
consumer of `Signal` was updated in the same controlled migration:
`BaseStrategy` (`src/strategies/sdk.py`) now takes a required `symbol`
constructor argument alongside `name`, stores it, and `emit_signal()`
attaches it to every `Signal` it builds automatically -- matching how a
`Strategy` instance already runs against exactly one instrument's
candles per `Backtester.run()`/`PaperBroker.submit_signal()` call, so
this is not a new constraint, just naming the one that already existed.
`EMACrossStrategy` threads `symbol` through to `BaseStrategy.__init__`.
`Backtester` needed no code change at all -- it already passes
`Signal` objects through into `BacktestResult.signals` untouched, so
`symbol` survives a backtest run for free. `ExperimentRegistry`
(`src/experiments/registry.py`) gained a `symbol` column on the
`signals` table, included in `save_signals()`'s INSERT and
`_row_to_signal()`'s reconstruction. `Trade` (`src/backtesting/models.py`)
was deliberately **not** changed to carry its own `symbol` -- a `Trade`
already traces back to the `Signal`s that opened/closed it via
`entry_signal_id`/`exit_signal_id`, and those signals (available via
`BacktestResult.signals`) already carry `symbol`; duplicating it onto
`Trade` too wasn't needed to close the actual gap this ADR targets.
`PaperBroker.submit_signal()`'s own separate `symbol` argument is
untouched -- the two are expected to agree in practice, but this round
doesn't add validation enforcing that, to avoid an unrequested behavior
change; a future round could add that check.

**Consequences:** Extension Cost (ADR-0014) for this addition is real
and intentionally not minimized: `Signal` being required (not defaulted)
means every existing call site across `src/strategies/sdk.py`,
`src/strategies/ema_cross.py`, `src/experiments/registry.py`, and every
test that constructed a `Signal` or a `BaseStrategy`/`EMACrossStrategy`
subclass needed a `symbol=` argument added -- a wide but shallow,
mechanical migration, not a design change at any call site. A pre-ADR-
0033 `experiments.db` file has no `symbol` column (`CREATE TABLE IF NOT
EXISTS` doesn't retrofit existing tables) -- a known, accepted gap
matching this codebase's existing no-migration-tooling posture (ADR-0004);
a fresh database picks up the column, an old one needs a manual `ALTER
TABLE` or to be recreated. `tests/test_architecture.py` now asserts
`Signal(...)` without `symbol` raises `TypeError`, and separately proves
the full lineage claim -- a `Signal` emitted by a real strategy, run
through a real `Backtester`, saved into and read back from a real
`ExperimentRegistry`, keeps both its `symbol` and its `id` intact.
`BaseStrategy` still only supports one symbol per strategy instance --
a strategy that genuinely wants to decide across multiple instruments in
a single run isn't supported by this SDK, tracked as a gap, not solved
here.

---

## ADR-0034: Research reporter surfaces LLM renderer failure instead of concealing it

**Status:** Accepted -- architecture review cleanup, pre-Sprint 6

**Context:** `ResearchReporter.run()` (ADR-0020) already had a
`rendered_by` field on `ResearchReport` distinguishing `"fallback"` from
`"claude"`, and already caught any exception from a
`ClaudeNarrativeRenderer` and fell back to the deterministic
`FallbackNarrativeRenderer` rather than losing the report. The
architecture review flagged that these two states -- "Claude was never
attempted because no API key/package was available" and "Claude was
attempted and actually failed (bad key, network error, malformed
response, timeout, API outage)" -- both collapsed to the identical
`rendered_by == "fallback"` value, with the actual exception silently
discarded in the `except` block. An operator watching this platform run
could not tell "everything is fine, Claude just isn't configured" apart
from "something is actually broken with the Claude integration" just by
looking at a `ResearchReport`.

**Decision:** Add `renderer_error: str | None = None` to `ResearchReport`
(`src/research/models.py`). `ResearchReporter.run()` (`src/research/reporter.py`)
now captures `str(exc)` into `renderer_error` in the `except` branch,
leaving it `None` on every other path (renderer succeeded, or the
fallback was chosen normally because no renderer was configured/available
in `_default_renderer()` -- that path never enters the `try`/`except` at
all). `rendered_by` and the deterministic `findings` are completely
unaffected by this change -- the fallback still always succeeds, `run()`
still never raises just because the optional AI prose failed, and the
LLM still cannot alter what facts exist, only how they're phrased
(ADR-0017, ADR-0020's own constraint, both preserved exactly). No new
dependency was added -- `anthropic` remains a lazy, optional import, not
in `requirements.txt`.

**Consequences:** Extension Cost (ADR-0014) for this fix: 2 files
changed (`src/research/models.py`, `src/research/reporter.py`), both
purely additive (`renderer_error` is a new field with a default, so no
existing caller of `ResearchReport(...)` or `ResearchReporter.run()`
breaks). `tests/test_research.py` gained coverage for: a successful
`ClaudeNarrativeRenderer` render end to end (`rendered_by == "claude"`,
`renderer_error is None`), the normal no-key fallback path
(`renderer_error is None`), a renderer that raises
(`renderer_error == str(exc)`), and a dedicated test proving the
deterministic `findings` are byte-for-byte identical whether or not the
renderer failed. Nothing yet *reads* `renderer_error` downstream (no
CLI/dashboard surfaces it to a human today) -- this ADR makes the
information available and tested, not necessarily acted upon yet;
wiring it into `atp doctor` or a future dashboard is a natural next
step, not built here.

## ADR-0035: Experiment specification, strategy version, and dataset identity

**Status:** Accepted -- Sprint 6

**Context:** The platform's long-term target (`ROADMAP.md`) is a
reproducible chain: experiment -> strategy/version -> parameters ->
dataset/version -> signals -> trades -> metrics -> attribution ->
report. Before Sprint 6, `ExperimentRegistry.log_experiment()` stored
`changed`/`metrics_before`/`metrics_after`/`decision`/`strategy_name` as
free-form dicts and a string, with no structured record of *what
produced* those numbers -- which exact strategy implementation, with
which parameters, against which exact data. Two concrete gaps made this
non-reproducible in practice, both explicitly raised in a Sprint 6
planning review:

1. **Strategy identity.** "EMA Cross, fast=12, slow=26" recorded today
   and the same label recorded after `EMACrossStrategy`'s
   implementation changes six months from now look identical in the
   registry, even though they are not the same experiment. Nothing
   distinguished "same name and parameters" from "same code".
2. **Dataset identity.** "SPY, 2020-01-01 to 2025-01-01" recorded today
   and the same descriptor recorded after the underlying vendor data is
   revised (or a stale cache serves different values) also look
   identical, even though the actual candles used may differ.

**Decision:** Establish the reproducibility seam, not the complete
system:

- **`ExperimentSpec`** (`src/experiments/spec.py`) -- a new, immutable
  dataclass capturing `strategy_name`, `strategy_version`,
  `strategy_params`, `symbol`, `interval`, `dataset_start`/
  `dataset_end` (from the actual candles used, not the requested
  range), `dataset_source`, `dataset_fingerprint`, `risk_config`
  (`RiskLimits`' two fields), and a currently-always-empty
  `backtest_config` placeholder (`Backtester.run()` takes no
  configuration today, ADR-0011; the field exists so adding one later
  doesn't widen `ExperimentSpec`'s shape). `ExperimentSpec.capture(strategy,
  candles, risk_limits, symbol=..., interval=..., dataset_source=...)`
  builds one from a strategy instance and the candles it ran against.
- **Strategy version = a hash of the strategy's own source**
  (`src/strategies/identity.py`, `strategy_version(cls)` ->
  SHA-256 of `inspect.getsource(cls)`). Considered and rejected: Git
  commit hashing (ties strategy identity to repository state, which the
  Sprint 6 review explicitly asked not to build yet) and a manually
  maintained `VERSION` string (relies on an author remembering to bump
  it -- exactly the failure mode motivating this ADR in the first
  place). A source hash is automatic and can't be forgotten, at the
  documented cost of also changing on a purely cosmetic edit (a
  comment, a docstring) -- a source-identity hash, not a
  semantic-identity one. This is a real, accepted trade-off, not
  something to silently work around later.
- **Dataset identity = a content hash of the actual candles used**
  (`src.utils.hashing.dataframe_fingerprint`, via
  `pandas.util.hash_pandas_object` over values + index + column names).
  Considered and rejected: a plain `(symbol, interval, start, end,
  source)` descriptor with no hashing, which cannot detect the exact
  failure mode this ADR exists for (identical descriptor, silently
  different underlying values); and a full dataset-versioning/snapshot
  system (explicitly out of scope -- "don't build a massive
  data-versioning system yet"). A content hash is the smallest thing
  that actually distinguishes "this exact data" from "this description
  of data," with no new storage.
- **Strategy registry** (`src/strategies/registry.py`,
  `@register_strategy("name")` / `get_strategy_class(name)` /
  `available_strategies()`) -- the same `@register_x` pattern already
  used by `src/indicators/registry.py` and `src/cli/registry.py`.
  `EMACrossStrategy` registers itself as `"ema_cross"`. This is what
  makes `ExperimentSpec.reconstruct_strategy()` possible: look up the
  class by the spec's stored `strategy_name`, construct it with
  `symbol=spec.symbol, **spec.strategy_params`.
- **`BaseStrategy.params`** (`src/strategies/sdk.py`) -- a new optional
  property, defaulting to `{}`, that a subclass overrides to expose its
  own tunable constructor arguments (`EMACrossStrategy.params` returns
  `{"fast": ..., "slow": ..., "confidence": ...}`). Deliberately not
  introspected automatically from `__init__`'s signature, and
  deliberately not added to the `Strategy` Protocol itself
  (`src/strategies/base.py`, untouched) -- both would be a real
  interface change forced onto every existing and future strategy;
  overriding one optional property is enough for a strategy that wants
  to participate in `ExperimentSpec.capture()`. A strategy that doesn't
  override it, or doesn't subclass `BaseStrategy` at all, still works
  everywhere it always did -- it just can't have its params
  auto-captured (`ExperimentSpec.capture()`'s `strategy_params=`
  argument lets a caller supply them explicitly instead).
- **Persistence**: `ExperimentRegistry.save_spec(experiment_id, spec)` /
  `get_spec(experiment_id)` (`src/experiments/registry.py`), a new
  `experiment_specs` table keyed by `experiment_id` (one spec per
  experiment, unlike the many-per-experiment `signals` table).
  Deliberately additive and separate from `log_experiment()`, the same
  reasoning `save_signals()` is separate (ADR-0016) -- `log_experiment()`'s
  signature and every existing test against it stay untouched.
- **Verification methods** on `ExperimentSpec`:
  `verify_strategy_version()` (does the currently-registered class for
  `strategy_name` still hash to the stored `strategy_version`?) and
  `verify_dataset(candles)` (does `candles` still fingerprint to the
  stored `dataset_fingerprint`?). Both return `bool` rather than raising
  -- a `False` is an expected, meaningful answer ("this drifted"), not
  an error condition.

**Explicitly not built this round** (the seam, not the system): no
dataset snapshotting, archival, or storage of prior fingerprints; no
strategy source-code archival (only its hash is kept, not the source
itself); no Git or package-version integration; no automatic re-run
scheduling or CI wiring; `Trade` still carries no `symbol` field of its
own (unchanged from ADR-0033 -- it still traces to symbol-carrying
`Signal`s); `PositionSizer`/`Backtester` remain unwired to each other
(ADR-0011/ADR-0021, unchanged); the complete experiment artifact graph
(trades, attribution, and research reports linked into the registry)
remains future work (`src/experiments/registry.py`'s own docstring,
`ROADMAP.md`).

**Consequences:** New files: `src/utils/hashing.py`,
`src/strategies/identity.py`, `src/strategies/registry.py`,
`src/experiments/spec.py`. Extension Cost (ADR-0014): 6 existing files
touched -- `src/utils/__init__.py` (exports), `src/strategies/sdk.py`
(`params` property), `src/strategies/ema_cross.py` (`@register_strategy`
+ `params` override), `src/experiments/__init__.py` (exports),
`src/experiments/registry.py` (`_SPECS_SCHEMA` + `save_spec`/`get_spec`),
and this file. New dependency edges: `src/experiments` now depends on
`src/strategies` and `src/risk` (for `ExperimentSpec`'s type
references) in addition to its existing `src/signals` dependency;
`src/strategies` and `src/experiments` both now depend on `src/utils`
for hashing. All three are downstream-depends-on-upstream, matching the
existing dependency direction -- no cycle, and neither `src/strategies`
nor `src/risk` gained any dependency on `src/experiments`
(`tests/test_pipeline_contract.py` and static review both confirm this).
`tests/test_hashing.py` (6 tests), `tests/test_strategy_registry.py` (9
tests), and `tests/test_experiment_spec.py` (12 tests) cover the new
modules directly; `tests/test_pipeline_contract.py` (3 tests) is the
single end-to-end contract test proving the whole chain -- Strategy ->
Signal -> Backtest -> Risk -> Execution -> Trade -> Performance ->
Attribution -> Experiment Registry -> spec -> reconstruction -- composes
and is reproducible, including a strategy rebuilt purely from its
stored `strategy_name`/`strategy_params` producing the identical
sequence of decisions (timestamp, symbol, direction, confidence) as the
original run.

## ADR-0036: `PaperBroker.submit_signal()` rejects a symbol/`Signal.symbol` mismatch

**Status:** Accepted -- Sprint 6

**Context:** ADR-0033 gave `Signal` a first-class, required `symbol`
field, but deliberately did not add validation that
`PaperBroker.submit_signal(signal, symbol, ...)`'s own `symbol`
argument actually agreed with `signal.symbol` -- flagged at the time as
a scope boundary, not an oversight, and reported as a known gap in
`PROJECT_STATE.md`'s Technical Debt. A Sprint 6 planning review called
this out directly: as the platform moves toward multi-asset trading,
silently allowing `submit_signal(signal_for_SPY, symbol="QQQ")` to
execute against the wrong instrument is exactly the kind of invariant
that becomes dangerous rather than theoretical.

**Decision:** `PaperBroker.submit_signal()` (`src/execution/engine.py`)
now raises `ValueError` immediately if `symbol != signal.symbol`,
before any other validation or side effect. The check runs first, ahead
of the existing `fill_price` validation, so a mismatched call never
touches cash or positions. The separate `symbol` parameter itself is
kept, not removed or made optional -- removing it would be an
interface change to a public method with many existing call sites, and
was explicitly out of scope ("no unnecessary redesign of existing
interfaces"); this ADR closes the gap by validating agreement between
the two, not by collapsing them into one.

**Consequences:** Extension Cost (ADR-0014): 1 file changed
(`src/execution/engine.py`), plus this entry. No existing caller breaks
-- every current call site across `src/`, `tests/`, and the examples in
docstrings already passes a `symbol` matching `signal.symbol` (verified
by grep before making this change); the new check only rejects inputs
that were already a latent bug. `tests/test_execution.py` gained
`test_submit_signal_rejects_symbol_mismatch_against_signal_symbol`,
asserting both the raised `ValueError` and that no cash/position side
effects occur on the rejected call. This closes the last item explicitly
flagged as deferred-not-forgotten in ADR-0033 and `PROJECT_STATE.md`'s
Technical Debt.

---

## ADR-0037: Sprint 6 close-out -- a second strategy and a worked-example script

**Status:** Accepted -- Sprint 6

**Context:** ADR-0035/ADR-0036 (Sprint 6 part 1) built `ExperimentSpec`,
the strategy registry, and the symbol/execution invariant, but left two
items open in `ROADMAP.md` before Sprint 6 could formally close: (1) no
second strategy had ever been registered, so the registry/`ExperimentSpec`
seam had only ever been exercised by `EMACrossStrategy` -- untested
against the actual claim that a genuinely different strategy plugs in
without touching core modules; (2) no script existed wiring a real
experiment through the full chain end to end -- only test fixtures had
ever driven the pipeline. Both are explicitly proof-point work, not new
capability: nothing in this ADR adds a feature the platform didn't
already have a seam for.

**Decision:** Two additions, both deliberately minimal.

1. **`RSIMeanReversionStrategy`** (`src/strategies/rsi_mean_reversion.py`,
   registered as `"rsi_mean_reversion"`). Chosen specifically because it
   is the *opposite* trading idea from `EMACrossStrategy` -- mean
   reversion, not trend following -- rather than a parameter variant of
   the same idea, so it's a real second data point for generalization,
   not a relabeled first one. Long-only: enters `LONG` the first time
   RSI drops to or below an `oversold` threshold (default `30.0`), exits
   to `FLAT` the first time RSI, while in that position, rises to or
   above an `overbought` threshold (default `70.0`). `<=`/`>=` semantics
   (not strict `<`/`>`) chosen so a threshold hit is never missed due to
   floating-point exactness. Rejects construction if `oversold` is not
   strictly less than `overbought`, or either is outside `(0, 100)`.
   Exposes a `params` property (`period`, `oversold`, `overbought`,
   `confidence`) the same way `EMACrossStrategy` does, for
   `ExperimentSpec.capture()`.
2. **`scripts/run_experiment.py`** -- a worked example wiring one real
   experiment through Strategy -> Backtest -> Risk -> Execution ->
   Attribution -> Research Report -> Experiment Registry (including
   `ExperimentSpec`). Deliberately placed in the pre-existing top-level
   `scripts/` directory, not `src/` -- `src/` would make this a
   permanent public API commitment the platform doesn't need yet
   (ADR-0021 already flags a reusable orchestration layer, a
   "PaperTradingLoop," as deferred future work; this script is not
   that). Split into a plain, network-free `run_experiment(...)` core
   function -- directly unit-testable, used identically for
   `"ema_cross"` and `"rsi_mean_reversion"` in
   `tests/test_run_experiment_script.py` -- and a thin `main()`/argparse
   CLI wrapper that is the *only* code path touching the network
   (`MarketDataService().get_history(...)`, imported lazily inside
   `main()` so importing the module for testing never requires it).

**Consequences:** Extension Cost (ADR-0014) for the new strategy: 2
files touched outside the new `rsi_mean_reversion.py` itself --
`src/strategies/__init__.py` (import + `__all__`, so the registry side
effect runs) and this file. Zero changes to `src/backtesting`,
`src/experiments/registry.py`, `src/attribution`, `src/research`, or
`src/broker` -- asserted directly by a new structural test,
`tests/test_architecture.py::test_second_strategy_required_no_changes_to_core_pipeline_modules`,
which greps those modules' source text for any mention of the new
strategy and fails if it finds one. This is the proof point Sprint 6's
closing principle asked for: adding a second strategy was an extension,
not a rewrite. `tests/test_rsi_mean_reversion_strategy.py` (14 tests)
mirrors `tests/test_ema_cross_strategy.py`'s structure and rigor
directly, including a full oversold-to-overbought cycle test against an
empirically-verified fixture (not hand-predicted RSI values).
`scripts/run_experiment.py` and `scripts/__init__.py` are new files with
zero Extension Cost of their own; `tests/test_run_experiment_script.py`
(7 tests) exercises `run_experiment()` against both registered
strategies plus `_parse_args()`, entirely network-free. Both
`ROADMAP.md`'s Sprint 6 close-out items are resolved; Sprint 6 itself is
marked complete in `ROADMAP.md`/`PROJECT_STATE.md` as of this entry.

---

## ADR-0038: Timeframe-agnostic architecture corrections

**Status:** Accepted -- pre-Sprint 7

**Context:** A pre-Sprint-7 architecture review asked a direct
question the platform had never explicitly tested for: can the same
research/strategy architecture built so far (Sprint 2-6) actually
support intraday and minute-scale trading later, or had it quietly
grown assumptions that only hold for daily bars? The product
requirement is explicit: the platform must support daily/swing
trading, intraday trading, and minute-scale automated strategies today,
with a future advanced execution/data layer potentially supporting
second-scale strategies -- but this is **not** an HFT platform, and
this round's job is correcting genuine daily-only assumptions, not
building tick feeds, order-book simulation, or session-aware
microstructure modeling.

An inspection of `src/data`, `src/signals`, `src/strategies`,
`src/backtesting`, `src/experiments`, and `src/attribution` (detailed
below) found the architecture already timeframe-agnostic almost
everywhere it mattered -- `Signal.timestamp`/`Trade.entry_time`/
`exit_time`/`ExperimentSpec.dataset_start`/`dataset_end` are all
`pd.Timestamp` (full precision, no date truncation); `Strategy`/
`BaseStrategy` never assume one signal per day or per candle; and
`Backtester` already treats `candles` as an ordered sequence of rows
with real timestamps, never as "one row = one trading day." Two
genuine gaps existed, both confirmed by direct inspection and reproduced
before being fixed, not assumed from first principles:

1. **Hidden daily assumption in Sharpe annualization.**
   `src/backtesting/metrics.py`'s `calculate_metrics()`/`sharpe_ratio()`
   annualized every backtest's Sharpe ratio with a flat, unconditional
   `periods_per_year=252` (trading days/year) regardless of what
   timeframe `candles` actually was. Correct for daily bars; silently
   wrong by orders of magnitude for intraday ones -- a 1-minute return
   annualized as if it were a full trading day's return drastically
   overstates Sharpe.
2. **Untyped, independently-hardcoded timeframe.** `ExperimentSpec.interval`
   was a bare `str` -- a caller could pass `"1D"`, `"daily"`, or a typo,
   and nothing would catch it. Worse, `scripts/run_experiment.py`'s
   `main()` fetched candles via `MarketDataService().get_history(...)`
   (which defaults to `DEFAULT_INTERVAL = "5m"`, already intraday) but
   called `run_experiment(..., interval="1d")` unconditionally --
   recording a timeframe on the `ExperimentSpec` that could silently
   disagree with what was actually fetched.

Everything else inspected -- `CacheManager`'s CSV round-trip (verified
empirically: a 1-minute `DatetimeIndex` survives `to_csv`/`read_csv`
with full precision), `YFinanceProvider._normalize()` (strips timezone,
never truncates time-of-day), the `Signal`/`Strategy`/`BaseStrategy`
contracts, and `Backtester`'s trade-extraction/position-series/equity-
curve logic -- was already correct and required no change. ADR-0006
(timezone consistency, still deferred) is unaffected and not
duplicated: this ADR adds no second, competing timezone system.

**Decision:** Two minimal, targeted corrections, extending existing
seams rather than redesigning them:

1. **`infer_periods_per_year(index)`** (`src/backtesting/metrics.py`,
   new function): estimates bars-per-year from the *median* gap between
   consecutive timestamps in a `DatetimeIndex` (robust to the occasional
   weekend/holiday gap in daily data), scaled by the historical
   252-trading-days-per-year constant. For daily bars (median gap = 1
   day) this reduces to exactly 252, unchanged from every existing
   test's prior behavior. For intraday bars it scales up accordingly --
   order-of-magnitude-correct, not exchange-session-precise (it doesn't
   know NYSE hours, holidays, or that some markets trade 24/7; modeling
   actual session length is real future work, not required to stop
   annualization from being silently wrong for non-daily bars).
   `calculate_metrics()`/`sharpe_ratio()`'s `periods_per_year` parameter
   changed from a hardcoded default `252` to `None` (infer), with an
   explicit int still accepted as an override; `Backtester.__init__`
   gained an optional `periods_per_year` passthrough for the same
   reason. No existing call site broke: every current caller either
   passes candles whose own spacing already implies 252, or didn't
   assert an exact Sharpe value in the first place (confirmed by review
   of every test referencing `sharpe`).
2. **`ExperimentSpec.interval: Interval`** (`src/experiments/spec.py`),
   typed via the pre-existing `src.data.base.Interval` enum instead of a
   bare `str` -- reusing the platform's own established timeframe
   vocabulary rather than inventing a parallel `Timeframe` type.
   `__post_init__` normalizes a plain string (e.g. `"1m"`) to `Interval`
   immediately via `Interval(self.interval)`, raising `ValueError` on an
   unrecognized value -- so every consumer of a constructed
   `ExperimentSpec` can rely on `.interval` always being the enum, and a
   typo is caught at construction time, not silently accepted.
   `capture()`'s `interval` parameter is typed `Interval | str` for the
   same convenience/safety balance. `ExperimentRegistry.save_spec()`/
   `_row_to_spec()` store/restore `spec.interval.value`/`Interval(row[...])`
   so the typed value round-trips through SQLite intact.
   `scripts/run_experiment.py` gained a `--interval` CLI flag (choices
   constrained to `Interval`'s own values), threaded through to *both*
   the actual `MarketDataService().get_history(..., interval=...)` call
   and `run_experiment(..., interval=...)` -- closing the exact
   fetch/record mismatch found above -- plus a fix to `main()`'s summary
   print, which previously called `spec.dataset_start.date()` (silently
   discarding time-of-day for an intraday run's own printed output).

**Explicitly not built this round** (per the sprint's own instruction):
tick feeds, order-book simulation, exchange co-location, high-frequency
execution, sub-millisecond latency infrastructure, or sophisticated
market microstructure models. `infer_periods_per_year()` is a calendar-
time approximation, not an exchange-session-aware one -- a future,
more precise annualization (accounting for actual NYSE hours, holidays,
non-24/7 markets) remains real future work, tracked in `ROADMAP.md`,
not solved here. Broker interfaces (`src/broker`) were reviewed and
left completely untouched -- no timeframe-related defect was found
there requiring even a minimal extension. `MarketDataService.get_candles()`'s
`start`/`end` parameters remain plain `date` (not `datetime`) request
boundaries -- this governs how wide a range is *requested*, not the
precision of the timestamps *returned*, which was already confirmed
full-precision; narrowing a request to a specific time-of-day boundary
is a separate, unrequested capability, not a bug this ADR needed to fix.

**Consequences:** Extension Cost (ADR-0014): 4 existing files touched
(`src/backtesting/metrics.py`, `src/backtesting/engine.py`,
`src/experiments/spec.py`, `src/experiments/registry.py`) plus
`scripts/run_experiment.py` (an already-existing script, not a new
module) and this file -- no new package created, since every fix
extended an existing seam (`Interval`, `calculate_metrics`) rather than
introducing a new abstraction. New contract tests
(`tests/test_timeframe_agnostic.py`, 6 tests) prove: the identical
`EMACrossStrategy` code and pipeline wiring run against daily and
1-minute fixtures unmodified; two signals six minutes apart within one
trading session both survive as a single precisely-timed trade; a
5.5-minute intraday hold attributes to exactly `pd.Timedelta(minutes=5,
seconds=30)`, not zero or a date-level bucket; `infer_periods_per_year()`
returns exactly 252 for daily spacing (unchanged) and two orders of
magnitude higher for 1-minute spacing; and `ExperimentSpec.interval`
is confirmed a typed `Interval` that round-trips through the registry
intact and rejects an unrecognized string. All 386 pre-existing tests
remain green, confirming no regression. The platform is now
architecturally ready for intraday and minute-scale research and
backtesting; live second-scale/HFT execution remains explicitly
unimplemented and out of scope, per the sprint's own instruction --
this ADR closes an architectural-readiness gap, not a functionality
gap, and `PROJECT_STATE.md`/`ROADMAP.md` are worded to keep that
distinction explicit rather than overstating what's actually wired up
end to end today (real intraday data has not yet been run through
`scripts/run_experiment.py` against a live provider, only through
synthetic fixtures).

## ADR-0039: Sprint 7 -- portfolio-aware, stop-based risk sizing and portfolio constraints

**Status:** Accepted -- Sprint 7. Refined by ADR-0040 (close-path
semantics, explicit short-margin representation, and a formalized
Risk/Execution boundary) -- nothing below was reverted or redesigned by
that follow-up, only made more explicit.

**Context:** Every trade sized so far (`PositionSizer`, ADR-0021) uses
`RiskLimits.allocation_per_trade_pct`: a fixed fraction of equity
committed to a trade, with no concept of a stop-loss or of how much the
trade could actually lose. ADR-0032 already renamed the field from
`risk_per_trade_pct` specifically to stop it being mistaken for a loss
figure. Sprint 7's product requirement is to add the thing that field
was never meant to be: given a signal, an entry price, and a stop
price, size the position from how much the account is willing to lose,
then check that size against portfolio-level constraints (total
exposure, per-symbol exposure, concurrent-position count) before it's
allowed to reach execution -- without touching `PositionSizer`,
`PaperBroker`, or any broker interface, since all three are stable,
tested, and still the right tool for what they already do.

**Decision:** Add a parallel, standalone risk-sizing path rather than
extending or replacing `PositionSizer`.

1. **`PortfolioRiskLimits`** (`src/risk/models.py`) is a new,
   separate dataclass from `RiskLimits` -- `risk_pct_per_trade`,
   `max_symbol_exposure_pct` (optional), `max_concurrent_positions`
   (optional), `min_quantity` (default `1`, matching `PaperBroker`'s
   integer-share contract). `RiskLimits` itself is untouched:
   `PortfolioRiskEngine` is constructed with *both* a `RiskLimits` (its
   `allocation_per_trade_pct` and `max_portfolio_exposure_pct` are
   reused, not duplicated) and a `PortfolioRiskLimits`. This keeps
   `test_risk_limits_is_not_a_maximum_loss_model` literally true
   forever -- the loss-based fields live in a different class, not a
   deleted assertion.
2. **`PortfolioRiskEngine.decide()`** (`src/risk/portfolio_risk.py`)
   implements a two-stage model. Stage A: `risk_amount = equity *
   risk_pct_per_trade`; `risk_quantity =
   floor(risk_amount / abs(entry_price - stop_price))` -- the
   spec's own worked example ($10,000 equity, 0.5% risk, $500 entry,
   $495 stop -> $50 risk amount, $5 risk/unit, quantity 10) is a
   direct unit test. Stage B computes four further quantities
   independently from the same proposed trade -- capital-affordable
   quantity, allocation-limited quantity, portfolio-exposure-limited
   quantity, symbol-exposure-limited quantity -- and takes
   `final_approved_quantity = min(risk_quantity, *those that apply)`.
   `risk_quantity` is a hard ceiling: nothing in Stage B can ever push
   the approved quantity above it (the sprint's own "REQUIRED
   ARCHITECTURAL INVARIANT," enforced by
   `tests/test_portfolio_risk.py`'s invariant test across five
   scenarios). A constraint that isn't configured, or doesn't apply to
   this trade (a `SHORT`'s `capital_quantity`, see below), contributes
   `None` and is excluded from the `min()` rather than an arbitrary
   large sentinel.
3. **Rejection is a `RejectionReason(str, Enum)`** (13 members:
   `INVALID_INPUT`, `INVALID_STOP`, `ZERO_STOP_DISTANCE`,
   `INSUFFICIENT_RISK_BUDGET`, `INSUFFICIENT_CAPITAL`,
   `ALLOCATION_LIMIT`, `MAX_PORTFOLIO_EXPOSURE`, `MAX_SYMBOL_EXPOSURE`,
   `MAX_CONCURRENT_POSITIONS`, `QUANTITY_BELOW_MINIMUM`,
   `POSITION_SCALING_NOT_SUPPORTED`, `UNSUPPORTED_POSITION_OPERATION`,
   `SYMBOL_MISMATCH`), never a free-form string -- the same "stable,
   serializable enum" pattern `Interval` established (ADR-0038). The
   same enum does double duty as `RiskDecision.limiting_constraint`: a
   tuple, not a single value, because when more than one
   independently-computed quantity ties at the binding minimum, all of
   them are exposed rather than picking one arbitrarily (spec section
   28.7). Validation runs in a fixed precedence order (input validity,
   then position-scaling, then the risk-budget floor, then the
   Stage-B ceilings, then the minimum-quantity floor, then the
   concurrent-position count) so an earlier, more fundamental rejection
   is always reported as itself rather than being masked by a later
   stage's arithmetic -- e.g. `risk_quantity < 1` is always
   `INSUFFICIENT_RISK_BUDGET`, never a capital/allocation reason,
   regardless of what those would separately have computed.
4. **`RiskDecision`** (`src/risk/models.py`) is a frozen dataclass
   carrying every intermediate quantity (`risk_amount`, `risk_quantity`,
   `capital_quantity`, `allocation_quantity`,
   `portfolio_exposure_quantity`, `symbol_exposure_quantity`), the
   final decision (`final_approved_quantity`, `limiting_constraint`,
   `rejection_reason`), and enough context (`resulting_exposure`,
   `explanation`) to reconstruct exactly why a decision came out the
   way it did without a debug log (spec section 17, "Auditability").
   `as_sizing_decision()` adapts it to the pre-existing `SizingDecision`
   shape so `PaperBroker.submit_signal(sizing_decision=...)` never has
   to learn about `RiskDecision` at all -- the connection to execution
   is an adapter, not a broker-interface change (spec section 14).
5. **Position scaling is not supported.** `PaperBroker` allows exactly
   one open position per symbol with no add/reduce mechanism
   (ADR-0022, unchanged). `decide()` rejects a call for an
   already-held symbol with `POSITION_SCALING_NOT_SUPPORTED` before any
   quantity math runs -- this is also why the concurrent-position-limit
   check only ever applies to genuinely new symbols, and why closing
   (`FLAT`) is submitted directly to `PaperBroker` exactly as before
   Sprint 7, never through `decide()` at all: there is nothing for a
   portfolio-exposure check to gate on a trade that only reduces
   exposure (spec Example E).
6. **Short-sale capital assumption.** `PaperBroker` credits cash
   immediately on a `SHORT` open with no margin/collateral requirement
   (`engine.py`'s existing docstring) -- so `capital_quantity` is
   `None` for `SHORT` decisions, a documented absence of a constraint
   under this platform's simplified model, not a fabricated number.
7. **Two coexisting `Position` models, deliberately.** The new
   `src.portfolio.position.Position` (symbol, side, quantity, entry
   price/timestamp, optional stop/current price, lifecycle,
   realized/unrealized P&L) is broker-independent and lives in the
   neutral `src/portfolio` package, next to the new `Portfolio`
   aggregate. `src.execution.models.Position` (the older, lighter
   fill-bookkeeping record `PaperBroker` already owns) is completely
   untouched. The two are kept in sync by
   `src.execution.portfolio_sync.apply_fill_to_portfolio()` -- a small
   glue function that reads a `Fill` `PaperBroker` already produced and
   calls `Portfolio.open_position()`/`close_position()` accordingly.
   This function has to live in `src/execution`, not `src/portfolio`:
   `Portfolio` cannot import `Fill`/`Order` without crossing the
   dependency-isolation boundary
   `test_portfolio_package_depends_on_nothing_else_in_this_codebase`
   protects, but `src/execution` is already allowed to depend on
   `src/portfolio` (it already does, for `AccountState`). A caller
   that wants both a `PaperBroker` simulation and a risk-facing
   `Portfolio` applies the same `Fill` to both, explicitly, via this
   one function -- a small, deliberate duplication favored over either
   rewriting `PaperBroker`'s tested internals or weakening the
   architecture test.
8. **`PositionLifecycle` has only `OPEN`/`CLOSED`.** "FLAT" is
   represented by absence from `Portfolio.positions`, not a third enum
   member -- a `Position` object inherently describes something already
   opened, so a `FLAT` member would mean either a nonsensical
   half-populated instance or dead code that never constructs one (see
   `position.py`'s module docstring for the full rationale). No
   partial-fill/scaling lifecycle states are added either, matching
   `PaperBroker`'s existing one-open-position-per-symbol limitation.
9. **`Portfolio` (`src/portfolio/models.py`) duplicates state
   `PaperBroker` already tracks, by design.** Rather than wrapping or
   modifying `PaperBroker`, `Portfolio` maintains its own `cash` and
   `_positions`, mirrored from the same `Fill` objects via
   `apply_fill_to_portfolio()`. `Portfolio.equity`/`total_exposure`
   reuse the same formulas `PaperBroker.account_state` already uses
   (cash plus each position's signed value at its `valuation_price`,
   which falls back to `entry_price` when no `current_price` is
   known -- no mark-to-market beyond what ADR-0022 already documents as
   absent). `to_account_state()` bridges back to the older
   `AccountState` shape for any caller (like `PositionSizer`) that only
   needs the two summary numbers.
10. **Naming stays clear of the deprecated field.** `risk_pct_per_trade`
    (on `PortfolioRiskLimits`) and `PortfolioRiskLimits` itself are
    named to be unambiguous next to `RiskLimits.allocation_per_trade_pct`
    and never resemble the removed `risk_per_trade_pct` (ADR-0032) --
    the whole point of that earlier rename was to stop "risk" and
    "allocation" from being confused, and Sprint 7 is exactly the
    moment a real risk concept was introduced, so the naming boundary
    has to hold precisely here.

**Explicitly not built this round** (per the sprint's own instruction):
Kelly criterion sizing, VaR/CVaR, correlation-aware exposure, portfolio
optimization, factor models, volatility targeting, dynamic hedging,
sophisticated margin modeling, market-impact modeling, order-book
simulation, HFT-style execution, real mark-to-market requiring a
current-price feed (`Position.current_price`/`unrealized_pnl` are
present but nothing sets them automatically), advanced broker-specific
risk handling, or any ML-based risk model. No existing constructor
signature changed: `RiskLimits`, `SizingDecision`, `PositionSizer`,
`AccountState`, `PaperBroker`, `src.execution.models.Position`,
`Order`, and `Fill` are all byte-for-byte unchanged from before this
sprint.

**Consequences:** Extension Cost (ADR-0014): three new files
(`src/portfolio/position.py`, `src/risk/portfolio_risk.py`,
`src/execution/portfolio_sync.py`), one new class added to each of two
existing files (`Portfolio` in `src/portfolio/models.py`;
`PortfolioRiskLimits`/`RejectionReason`/`RiskDecision` in
`src/risk/models.py`), and updated `__init__.py` exports in
`src/portfolio`, `src/risk`, `src/execution` -- no existing class
modified, no existing test's behavior changed. New tests:
`tests/test_portfolio_position.py` (27 tests: `Position` validation and
computed properties, `close()`, and `Portfolio` construction,
open/close cash math for both long and short, multi-position support,
symbol-exposure-for-an-unheld-symbol-is-zero, and the
`to_account_state()` bridge), `tests/test_portfolio_risk.py` (36 tests:
`PortfolioRiskLimits` validation, the spec's own worked sizing example,
invalid-stop and zero-stop-distance cases, floor-not-round quantity
rounding, every individual constraint, the position-scaling and
concurrent-position distinctions, `RiskDecision` field contents for
both approved and rejected outcomes, all six of the spec's mandatory
worked examples A-F, and the architectural invariant across five
scenarios), and `tests/test_sprint7_integration.py` (6 tests: the full
Signal -> `PortfolioRiskEngine` -> `PaperBroker` -> `Fill` ->
`apply_fill_to_portfolio` -> `Portfolio` pipeline for an approved trade,
a full position closure, a rejected decision that never reaches
execution, the pre-existing signal/execution symbol-mismatch invariant
holding with a `RiskDecision` in the loop, three concurrent positions
opened end to end, and a pre-existing-position scenario proving new
trades are still constrained by exposure the risk engine didn't itself
create). `tests/test_architecture.py` gained
`test_risk_modules_do_not_import_src_broker_or_src_execution`,
confirming the dependency direction stays Portfolio -> Risk ->
Execution -> Broker; the pre-existing
`test_portfolio_package_depends_on_nothing_else_in_this_codebase`
continues to pass with `position.py` present, since `Portfolio` and
`Position` import only from within `src/portfolio` itself. All
pre-existing tests remain green -- no regression in `PositionSizer`,
`PaperBroker`, or any broker module. Strategies still express intent
only (a `Signal` with a direction and confidence); `PortfolioRiskEngine`
is the only new place that decides size and portfolio admission,
preserving the strategy/risk/execution separation the rest of the
platform already relies on.

## ADR-0040: Sprint 7 cleanup -- explicit close-path semantics, explicit short-margin representation, and a formalized Risk/Execution boundary

**Status:** Accepted -- Sprint 7 cleanup (post-ADR-0039)

**Context:** A review of the Sprint 7 implementation (ADR-0039) raised
three concerns, none disputing the substantive risk/portfolio work
itself:

1. `PortfolioRiskEngine.decide()` raised `ValueError` on a `FLAT`
   signal with no explicit, structured alternative for a caller that
   actually wants to know "may this close proceed?" -- the close path
   was correct in behavior (a `FLAT` signal already went straight to
   `PaperBroker`, bypassing risk entirely) but not expressed as its own
   architectural concept anywhere in `src/risk` itself.
2. `RiskDecision.capital_quantity` being `None` for a `SHORT` -- a
   deliberate, documented absence of a margin model (ADR-0039) -- was
   only distinguishable from "unlimited capital" by reading prose. The
   review asked for that distinction to be machine-visible and typed,
   not just written down.
3. The Risk/Execution/Portfolio responsibility boundary that already
   existed in practice (confirmed by re-reading `src/execution/engine.py`
   line by line: `PaperBroker.submit_signal()` consumes
   `sizing_decision.position_size` directly and performs no risk math of
   its own) had never been stated as an explicit contract, nor was the
   "execution may never increase what risk approved" invariant enforced
   anywhere in code -- it held by inspection, not by construction.

This is a cleanup pass, not a redesign: every concern above is answered
by adding a small, explicit surface to what ADR-0039 already built, not
by changing any existing behavior. `RiskLimits`, `SizingDecision`,
`PositionSizer`, `AccountState`, `PaperBroker`, `Portfolio`,
`Position`, `apply_fill_to_portfolio()`, and every existing
`PortfolioRiskEngine.decide()` call site are unaffected -- `decide()`
still raises on `FLAT`, exactly as before.

**Decision:**

1. **`PortfolioRiskEngine.decide_close(signal, portfolio, quantity=None)`**
   (`src/risk/portfolio_risk.py`) is the new, symmetric counterpart to
   `decide()` for exit intent -- accepting only `FLAT` (raising on
   anything else, the strict mirror of `decide()` refusing `FLAT`).
   It is a lookup-and-permit operation, never a sizing one: no risk
   budget is computed (`risk_pct`/`risk_amount` are `0.0` on the
   returned decision), and `RiskLimits.max_portfolio_exposure_pct`/
   `PortfolioRiskLimits.max_symbol_exposure_pct` are never consulted --
   a close can proceed even when the portfolio is already over either
   limit, since reducing exposure can never be the thing that breaches
   an exposure limit. No open position in the signal's symbol ->
   `RejectionReason.NO_POSITION_TO_CLOSE` (a new enum member, never
   fabricated as a close order and never conflated with a risk-limit
   rejection). A position exists -> approved for its full quantity
   only; `PaperBroker`/`Portfolio` support no partial reduction this
   sprint (ADR-0022), so a `quantity` argument other than the position's
   own full size is rejected with the existing
   `RejectionReason.UNSUPPORTED_POSITION_OPERATION` -- partial-close
   support is not fabricated to satisfy the request. `decide_close()`
   never submits an order itself, exactly like `decide()` -- the actual
   close still goes through `PaperBroker.submit_signal()` directly
   (unchanged), with `apply_fill_to_portfolio()` syncing `Portfolio`
   from the resulting `Fill`, exactly as for an entry.
2. **`RiskDecision` gained `is_close: bool = False`** so a close
   decision is unambiguously distinguishable from an entry decision in
   any audit record, without breaking any existing construction
   (defaulted). On a close decision, `entry_price`/`stop_price` echo
   the *existing* position's own recorded values (traceability), never
   a newly-proposed risk boundary.
3. **`CapitalConstraintModel(str, Enum)`** (`MODELED`/`NOT_MODELED`,
   `src/risk/models.py`) makes the short-margin absence machine-visible.
   `RiskDecision` gained `capital_model: CapitalConstraintModel | None
   = None`, populated by `PortfolioRiskEngine.decide()` for every entry
   decision (`MODELED` for `LONG`, `NOT_MODELED` for `SHORT`) and left
   `None` for a close decision (capital affordability isn't a question
   `decide_close()` asks). `capital_quantity is None` must now always be
   read alongside `capital_model` -- the pair together say "no capital
   ceiling was computed," never "capital is unlimited for this trade."
   Documented explicitly, in `portfolio_risk.py`'s own module docstring,
   what a `SHORT` *is* still constrained by (risk budget, stop
   distance, total exposure, symbol exposure, allocation limits,
   concurrent-position limit) versus the one thing genuinely unmodeled
   (broker-realistic margin/borrow/financing).
4. **`RiskDecision.to_trade_intent(quantity=None) -> ApprovedTradeIntent`**
   (`src/risk/models.py`) formalizes the Risk -> Execution handoff.
   `ApprovedTradeIntent` is a new frozen dataclass (`symbol`,
   `direction`, `quantity`, `entry_price`, `stop_price`, `signal_id`,
   `risk_decision`) that composes rather than duplicates: the full
   audit record stays in the referenced `risk_decision`, not copied.
   `to_trade_intent()` raises `ValueError` if the decision was not
   approved, or if the requested `quantity` exceeds
   `final_approved_quantity` -- the hard invariant
   `execution_quantity <= risk_approved_quantity` enforced in code, at
   construction, not left to convention (`tests/test_portfolio_risk.py::
   test_to_trade_intent_rejects_a_quantity_exceeding_risk_approval`).
   This coexists with, and does not replace, `as_sizing_decision()` --
   `PaperBroker.submit_signal()` still consumes the older
   `SizingDecision` shape unchanged (ADR-0039's own reasoning, section
   14 of the Sprint 7 spec, "without redesigning the broker
   interfaces"). `ApprovedTradeIntent` is the more complete, explicit
   contract for reasoning about and auditing the boundary itself, not a
   new input this cleanup wires `PaperBroker` up to consume.
5. **`RiskDecision` gained `signal_id: UUID | None = None`**, populated
   by both `decide()` and `decide_close()` from `signal.id` -- closing
   the audit-trail gap where a `RiskDecision` carried no reference back
   to the signal that produced it.
6. **Responsibility boundary stated explicitly, and one edge locked by a
   new architecture test.** `src/risk`'s module and class docstrings
   now state plainly: Risk decides whether and how much a trade may
   proceed and never submits orders, calls a broker, or mutates
   `Portfolio`; Execution carries out an already-approved trade and
   never recalculates risk or exceeds the approved quantity; Portfolio
   holds resulting state and makes no trade-permission decisions.
   `tests/test_architecture.py::test_execution_does_not_duplicate_risk_sizing_logic`
   statically confirms `src/execution` never imports the risk-computation
   symbols (`PortfolioRiskEngine`, `PortfolioRiskLimits`, `RiskLimits`,
   or anything from `src.risk.portfolio_risk`) -- only the plain
   `SizingDecision` data shape, which it has always legitimately
   depended on. The pre-existing
   `test_risk_modules_do_not_import_src_broker_or_src_execution`
   already proved the reverse edge (Risk cannot call Execution/Broker,
   since it cannot even import them); a new
   `test_engine_decide_signature_carries_no_broker_or_execution_reference`
   reinforces this at the call-signature level -- `decide()`/
   `decide_close()` are never handed a broker or execution object to
   act through in the first place.

**Explicitly not built this round** (per the cleanup's own scope):
no new margin engine, no broker-specific short-selling rules, no
position scaling (a same-symbol `decide()` call and any non-full
`decide_close()` quantity both remain rejected exactly as ADR-0039 left
them), no real mark-to-market, no broker interface changes, no change
to `PositionSizer`/`allocation_per_trade_pct` semantics, no redesign of
`Signal`/`AccountState`/`PortfolioRiskEngine`'s existing `decide()`
method, and no `ExecutionResult` wrapper type: `Fill` (execution) and
`Portfolio`/`Position` (state) already are the distinct, structured
outputs Execution and Portfolio expose respectively; introducing a
parallel `ExecutionResult` this round with no actual consumer would
itself be exactly the kind of speculative abstraction this cleanup was
scoped to avoid. `PaperBroker.submit_signal()` already reports failure
by raising (unchanged) rather than returning an error variant -- this
cleanup does not change that. Sprint 8 was not started.

**Consequences:** Extension Cost (ADR-0014): two existing files
extended (`src/risk/models.py`: `NO_POSITION_TO_CLOSE`,
`CapitalConstraintModel`, three new `RiskDecision` fields,
`ApprovedTradeIntent`, `to_trade_intent()`; `src/risk/portfolio_risk.py`:
`decide_close()`, `capital_model`/`signal_id` threaded through
`decide()`), plus updated `src/risk/__init__.py` exports -- no existing
class, method, or field removed or changed in place;
`src/execution/engine.py` and `src/portfolio/*.py` are byte-for-byte
unchanged. New tests: 22 added to `tests/test_portfolio_risk.py`
(close-path Cases A-D and the required-tests list, short-margin
representation and its effect on SHORT trades, `to_trade_intent()`'s
invariant enforcement), 4 added to `tests/test_sprint7_integration.py`
(the close path end to end via `decide_close()` + `PaperBroker` +
`apply_fill_to_portfolio()`, close permitted over a breached exposure
limit, no-position-to-close never reaching execution, and "close is
execution-owned" proving `PaperBroker.submit_signal()` needs no
sizing_decision for a `FLAT` signal), 1 added to
`tests/test_architecture.py` (execution doesn't duplicate risk-sizing
logic). Sandbox-verified: 497 passed (up from ADR-0039's 470), same 2
known environment-only artifacts. Confirmed via real `pytest` on the
dev machine (Python 3.14.6, pytest 9.1.1): 499 passed in 1.18s, all
green -- both sandbox-only artifacts passed for real, confirming they
were environment-only, not regressions. This cleanup is verified.

---

## ADR-0041: Sprint 8 -- Market Data Integrity & Session Awareness

**Status:** Sandbox-verified, pending real-`pytest` confirmation on the
dev machine.

**Context:** Three data-integrity questions had sat open across the
codebase's own history, each one deliberately deferred rather than
guessed at: ADR-0006 (Sprint 1, "Data validation layer for candle
data") proposed validation but was never implemented; ADR-0007 (Sprint
1, "Incremental cache fetch") proposed range-aware caching but was
never implemented; and a bare timezone policy question -- referenced
by name in ADR-0019 ("session buckets only mean something if candle
timestamps are reliably in market-local time, which ADR-0006... hasn't
landed") and ADR-0038 -- had blocked session-of-day attribution for two
sprints running. Meanwhile `MarketDataService` (ADR-0002/ADR-0008)
served whatever a provider returned with no re-validation on a cache
hit, `dataframe_fingerprint()` (ADR-0035) hashed whatever DataFrame
happened to be passed with no canonical form guaranteed, and nothing
in the codebase modeled a trading session at all -- every intraday
timestamp was implicitly "whatever the provider felt like returning,"
extended hours included.

**Decision:** Resolve all three, plus add the session/calendar layer
they each depend on:

1. **Timezone policy (resolves ADR-0006's core question).** Internal
   candle timestamps are timezone-aware UTC, always. A new choke point,
   `canonicalize_candles()` (`src/data/canonical.py`), pins row order,
   column order (`REQUIRED_COLUMNS`), numeric dtype (float64
   throughout, including volume -- a CSV cache round-trip silently
   promotes an int column the moment it holds a NaN, so pinning float64
   up front keeps a cache-read and a fresh fetch dtype-identical), and
   timezone (a naive index is localized as UTC -- the correct
   convention for a date-only daily/weekly/monthly bar with no
   meaningful time-of-day; an aware index is converted). `DataProvider`'s
   contract (`src/data/base.py`) was loosened accordingly: a provider
   may return either convention honestly, and `MarketDataService`
   canonicalizes uniformly regardless. `YFinanceProvider._normalize()`
   no longer strips timezone information -- it used to discard the
   tz-aware intraday timestamps yfinance actually returns, silently.
   This was a full migration, not scoped to new surfaces only
   (confirmed explicitly before implementation): it reaches
   `DataProvider`/`MarketDataService`'s return contract itself. The
   actual blast radius was much smaller than the pre-implementation
   estimate of ~15-25 affected test files -- empirically, exactly one
   file (`tests/test_market_data.py`) needed fixture changes, because
   every other existing test builds candles directly and hands them to
   `Backtester`/strategies without ever routing through
   `MarketDataService`, so the naive/aware distinction never reached
   them.

2. **Validation (implements ADR-0006).** `validate_candles()`
   (`src/data/validation.py`) runs on every DataFrame
   `MarketDataService` returns -- fresh fetch or cache hit alike.
   Structural failures (missing/duplicate/unordered timestamps, a
   naive index, missing OHLCV columns or values, empty symbol) raise
   `StructuralValidationError`; financial-sanity failures (`high <
   max(open, close)`, `low > min(open, close)`, `high < low`, negative
   volume) raise `FinancialSanityError` -- both new subclasses of a new
   `DataValidationError` base (`src/data/exceptions.py`). Neither kind
   is repaired, only rejected. Zero volume is explicitly *not* a
   sanity violation (real, if unusual) -- it's recorded as
   `suspicious`, never raised. Not an anomaly detector: exactly the two
   checks the sprint's spec named, nothing statistical.

3. **Gap detection (Sprint 8, new).** Given a `TradingCalendar`,
   `validate_candles()` builds the expected timestamp grid for the
   sessions a dataset's own range covers and diffs it against what's
   present. A missing bar inside an open session is a gap
   (`ValidationReport.gaps`, non-fatal); a missing bar outside any
   session (nights, weekends, holidays) never is. One subtlety this
   surfaced during implementation: a daily bar's timestamp is a
   calendar-date marker, not a real local-exchange instant (per the
   canonicalization convention above) -- converting it to
   exchange-local time before taking `.date()` shifts it to the
   *previous* calendar date for any exchange behind UTC, which would
   have produced spurious gaps at exactly the boundary the tests for
   this caught. Daily gap detection compares raw UTC dates for this
   reason; intraday gap detection converts to exchange-local time
   first, correctly, since intraday timestamps are real instants.
   Weekly/monthly bars are not gap-checked at all -- deliberately: how
   many weekly bars *should* exist across a holiday-shortened week
   isn't well-defined without more calendar work than this round
   scoped.

4. **Session/calendar abstraction (Sprint 8, new).** A new top-level
   package, `src/calendar/` (`TradingCalendar(ABC)`: `is_session`,
   `session_open`/`session_close`, `sessions_between`, `session_date`),
   mirrors `src/data`'s `DataProvider` seam. `NYSECalendar` is the only
   concrete implementation: regular session only (09:30-16:00
   America/New_York), holidays sourced from a small, explicit,
   rule-based table (`_nyse_holidays()`) rather than a hardcoded date
   list or a calendar-library dependency -- each of NYSE's nine annual
   closures is a fixed calendar rule (e.g. "third Monday in January";
   Good Friday via the standard Anonymous Gregorian Easter algorithm),
   computed on demand for any year in the explicit, bounded
   `SUPPORTED_YEARS` range (2015-2035) rather than enumerated by date.
   Confirmed before implementation as the right trade-off given the
   sprint's explicit "don't build a full exchange-calendar platform"
   instruction: correct for every supported year without per-year
   maintenance, zero new dependencies, at the cost of not modeling
   NYSE's rare unscheduled closures (a national day of mourning, say)
   -- a documented, known limitation, not a silent gap. `is_session`/
   `session_open`/`session_close`/`sessions_between` all raise
   `ValueError` for a year outside `SUPPORTED_YEARS` rather than
   silently answering for a year this logic was never verified against.

5. **Dataset identity, hardened.** `dataframe_fingerprint()`
   (`src/utils/hashing.py`, ADR-0035) is unchanged -- it always ran
   against whatever DataFrame it was given, and that's still correct.
   What changed is that `canonicalize_candles()` is now the *only*
   thing ever fingerprinted: `MarketDataService` canonicalizes before
   hashing, from both the fresh-fetch and cache-hit paths, so
   "identical canonical candle content" and "identical content hash"
   are now actually the same claim regardless of which path served the
   data -- closing the gap ADR-0035 always intended to close but never
   fully guaranteed on its own.

6. **Incremental cache (implements ADR-0007).** The cache key moves
   from `(symbol, interval, start, end)` to `(symbol, interval)`,
   storing the widest span fetched so far. A request whose `start`
   falls within the cached span but whose `end` extends past it fetches
   only the new tail and merges it in; a request whose `start` falls
   *before* the cached span falls back to a full refetch of the whole
   newly requested range, deliberately, rather than building general
   interval-merge logic for the less common case (the sprint's own
   "architectural seam, not a full distributed data system"
   instruction). The full merged history -- not the caller's requested
   window -- is what's written to cache and what validation/gap
   detection run against; the caller's window is sliced out only at
   the very end, after canonicalization, validation, and session
   filtering. Every write is preceded by a passing validation, so bad
   data is never persisted to disk in the first place.

7. **Regular-session filtering.** `MarketDataService.get_candles()`/
   `get_history()`/`get_dataset()` gained a `session: SessionPolicy`
   parameter (`REGULAR` default, `ALL` opt-out) -- a no-op for
   daily+ intervals (no time-of-day to filter), and for intraday
   intervals, filters to 09:30-16:00 America/New_York on actual
   trading days via `NYSECalendar`. Extended-hours support is an
   explicit, reserved, unimplemented future value, not something
   accidentally half-working today.

8. **The Canonical Dataset (Sprint 8's architectural invariant).** A
   new `CandleDataset` (`src/data/models.py`) -- candles plus
   symbol/interval/provider/timezone/session-policy/requested-range/
   dataset-range/content-hash/validation-report -- is what
   `MarketDataService.get_dataset()` (new, additive) returns.
   `get_candles()`/`get_history()` keep their exact pre-Sprint-8
   signatures and still return a plain, now-canonicalized-and-validated
   `pd.DataFrame` -- nothing that already called them needed to change.
   `Backtester.run()` gained one new optional `dataset: CandleDataset
   | None` parameter; when supplied, `BacktestResult.dataset_identity`
   (a new, `None`-by-default field) records the portable subset --
   symbol, timeframe, content hash, session/timezone convention -- a
   backtest actually ran against, satisfying the sprint's "a backtest
   should be able to identify... dataset identity/hash" requirement
   without touching any existing `Backtester.run(strategy, candles)`
   call site.

**Explicitly not built this round** (per the sprint's own non-goals,
confirmed unchanged throughout implementation): no market-data
warehouse, tick or order-book data, corporate-actions infrastructure, a
complete exchange-calendar platform, multiple commercial providers,
real-time streaming, live trading orchestration, or statistical
anomaly detection. Also not built, as narrower in-round scope
decisions: partial-day/pre-market/after-hours session support (the
`SessionPolicy` enum reserves the concept, implements none of it);
general interval-merge caching for a `start` before the cached span;
gap detection for weekly/monthly bars; a calendar-library dependency
(the hand-rolled rule table was chosen instead, explicitly).

**Consequences:** Extension Cost (ADR-0014): one new top-level package
(`src/calendar/`: `base.py`, `nyse.py`), three new modules in
`src/data/` (`canonical.py`, `validation.py`, `models.py`), and six
existing files extended (`src/data/base.py`'s `DataProvider` contract
docstring; `src/data/yfinance_provider.py`'s `_normalize()`;
`src/data/exceptions.py`'s three new error types;
`src/data/service.py`'s cache/fetch/validate/filter pipeline and new
`get_dataset()`; `src/data/__init__.py`'s exports;
`src/backtesting/models.py`'s new `dataset_identity` field and
`src/backtesting/engine.py`'s new `dataset` parameter) -- no existing
class, method, or field removed, and `get_candles()`/`get_history()`
are unchanged in signature. New tests: `tests/test_calendar.py` (19),
`tests/test_data_validation.py` (24), `tests/test_data_canonical.py`
(13), 11 added to `tests/test_market_data.py` (tz-aware output,
`get_dataset()` metadata/hash/identity, incremental fetch's three
branches, session filtering's three cases, cache/fresh-fetch hash
equivalence), 3 added to `tests/test_architecture.py` (the "no raw
provider access outside `src/data`" invariant, `src/calendar`'s
leaf-dependency status, and the positive confirmation that
`MarketDataService` is where the provider import actually lives).
Sandbox-verified: 567 passed (up from ADR-0040's 497 sandbox-passed /
499 real-passed), same 2 known environment-only artifacts.

**Correction (post-commit cleanup):** the first real `pytest` run on
the dev machine (Python 3.14.6) actually returned **1 failed, 568
passed**, not the "569 passed, 0 failed" this entry originally (and
wrongly) claimed -- that claim was written before the corresponding
real run had actually happened, and is the mistake this correction
exists to document. The failure:
`test_data_canonical.test_canonicalize_is_idempotent`'s
`assert_frame_equal()` (default `check_freq=True`) compared two
canonicalization passes whose `DatetimeIndex.freq` differed (`<Day>`
vs. `None`) even though values/columns/dtypes/content-hash were all
confirmed identical -- `tz_localize()` (first pass) and `tz_convert()`
(second pass) aren't guaranteed to preserve `.freq` identically across
pandas versions (the sandbox's pandas 2.3.3 doesn't reproduce it; the
dev machine's does), and `.freq` isn't part of what
`dataframe_fingerprint()` hashes.

The first fix attempt relaxed the test itself (`check_freq=False`).
That was reverted: `.freq` is pandas-internal bookkeeping, not candle
content, so letting it silently differ between two "canonical" outputs
is exactly the kind of accidental non-determinism this module exists
to prevent -- allowing it, even just in a test comparison, understates
what "canonical" is supposed to guarantee. The real fix is in
`src/data/canonical.py`: `canonicalize_candles()` now explicitly pins
`DatetimeIndex.freq` to `None` on its output, so two canonicalization
passes can never disagree on it regardless of which pandas version, or
which of `tz_localize()`/`tz_convert()`, produced the index. Three
regression tests added: `test_canonicalize_clears_inferred_datetimeindex_freq`
(canonical output never carries inferred freq metadata),
`test_hash_of_canonical_output_is_stable_under_a_second_canonicalization_pass`
(the hash-level idempotence restatement), and
`test_incremental_fetch_produces_the_same_hash_as_a_complete_fetch`
(closes a real coverage gap: the existing incremental-fetch tests
checked call counts and date ranges, not that the assembled result
hashes identically to a one-shot fetch of the same range).

Sandbox-reverified after the fix: **570 passed** (567 + 3 new
regression tests), same 2 known environment-only artifacts, no other
breakage. **Confirmed via real `pytest` on the dev machine: 572
passed, 0 failed, 0 errors, all green** -- both sandbox-only artifacts
passed for real (570 sandbox + 2 environment-only = 572), confirming
the fix is genuine and this sprint is now actually verified, not just
sandbox-verified. This sprint is confirmed and ready to commit.

---

## ADR-0042: Sprint 9 -- Analytics & Dashboard

**Status:** Confirmed via real `pytest` on the dev machine: **688
passed, 0 failed, all green.**

**Context:** `ROADMAP.md` named two different candidates for "Sprint
9" (Analytics & Dashboard vs. AI); the sprint's own spec explicitly
resolved this as Candidate A -- Analytics & Dashboard is Sprint 9, and
Candidate B -- AI is explicit future work, not built this round (no
ML/LLM signal generation, no AI agents, no AI-driven strategy
selection anywhere in this change). Investigating the existing system
first (`src/backtesting`, `src/experiments`, `src/portfolio`,
`src/execution`, `scripts/run_experiment.py`) surfaced a real gap: the
Experiment Registry (ADR-0012/ADR-0016/ADR-0035) persisted a `Signal`
list and an `ExperimentSpec`, but never a backtest's `Trade` list, its
`equity_curve`, or the `Portfolio` its signals resolved to after paper
execution -- even though `run_experiment()` computes all three in
memory before discarding them. A dashboard that wants to browse *past*
experiments (not just ones just run in the same process) needs that
data actually saved.

**Decision:**

1. **Registry extension, additive.** Four new tables mirror the
   existing `save_signals()`/`save_spec()` pattern exactly:
   `experiment_trades`, `experiment_equity_curves`,
   `experiment_portfolios`, `experiment_research_reports`, each written
   once by `run_experiment()` immediately after `log_experiment()`
   returns an id. `get_trades()`/`get_equity_curve()`/`get_portfolio()`/
   `get_research_report()` return an explicit "nothing here" value
   (`[]`, an empty `pd.Series`, or `None`) for any experiment logged
   before Sprint 9 -- never a fabricated one. Chosen over a
   session-scoped-only alternative (analytics only ever computed from
   an in-memory `BacktestResult`, confirmed via `AskUserQuestion` with
   the user selecting the registry-extension option) because the whole
   point of a dashboard is inspecting experiments that aren't in memory
   any more.

2. **`Portfolio.reconstruct()` -- a new, explicit restore path.**
   `ExperimentRegistry.get_portfolio()` needs to rebuild a `Portfolio`
   from already-known state (cash, open positions, closed positions)
   without re-deriving anything. `Portfolio.open_position()`/
   `close_position()` are the wrong tool for this: they simulate a
   *new* fill (deriving `cash` from a price, rejecting a symbol that's
   already open or already closed) -- exactly backwards for loading a
   snapshot that was already correct the moment it was saved. A new
   `@classmethod Portfolio.reconstruct(cash, positions, closed_positions)`
   (`src/portfolio/models.py`) bypasses that simulation and trusts the
   given state directly. (The first implementation attempt reached into
   `Portfolio`'s private `_positions`/`_closed_positions` from
   `src.experiments.registry` directly -- caught during self-review as
   a package-boundary violation and replaced with this proper public
   classmethod before it was ever tested.)

3. **`src/analytics/` -- deterministic metrics, no Streamlit
   dependency.** `models.py` defines `Metric`/`MetricStatus` (every
   computed number carries an explicit `OK`/`UNDEFINED` status plus a
   reason -- never a silent `0`, `inf`, or `nan` when the underlying
   math has no answer: no trades, zero variance, no equity
   observations, no losing trades for profit factor). `metrics.py`
   holds pure functions over `list[Trade]` + `pd.Series` equity curves
   -- `total_pnl`/`total_return` read directly off the equity curve (so
   they're genuine dollar/fractional totals for the run); win rate,
   profit factor, expectancy, and average/largest winner/loser are
   computed from `Trade.return_pct` (a fraction of entry price) rather
   than a dollar P&L, since this backtester (ADR-0011) has no
   persistent per-trade share count to derive a dollar amount from
   without inventing one. Sharpe and volatility both reuse
   `src.backtesting.metrics.infer_periods_per_year` (ADR-0038) for
   timeframe-aware annualization -- never a blind `sqrt(252)` -- and
   both return the exact `periods_per_year` used alongside the value,
   for the "auditable back to periodicity + annualization convention"
   requirement. `max_drawdown()` is built on a new `drawdown_curve()`
   (the running drawdown at every point, not just its minimum) so a
   caller needing the full series for a chart never has to re-derive
   the drawdown formula itself. `service.py`'s `AnalyticsService`
   is the one thing a caller actually talks to:
   `analyze_backtest(result, spec=None)` for an in-memory result,
   `analyze_experiment(registry, experiment_id)` for a persisted one
   (gracefully degrading to all-`UNDEFINED` metrics for a pre-Sprint-9
   experiment with no saved trades/equity, rather than crashing or
   fabricating). `compare_experiments()` builds a `ComparisonResult`
   that flags (never blocks) a material difference in symbol,
   interval, dataset fingerprint, or strategy-implementation hash
   across the rows compared -- deliberately no composite "best
   strategy" score or ranking anywhere in this package.
   `valuation.py`'s `PortfolioValuationService.value(portfolio,
   price_lookup)` is strictly read-only (never assigns
   `Position.current_price`, never calls a `Portfolio` mutator) --
   calling it twice never changes the `Portfolio` it was given. When
   any open position lacks a current price, the *aggregate* Metrics
   (market value, unrealized P&L, equity, exposure, total P&L) become
   `UNDEFINED` rather than a partial sum that silently omits the
   unpriced position -- the same "don't fabricate" principle applied
   to totals, not just individual numbers; the per-position breakdown
   still shows exactly which symbols were priced. `latest_price()` and
   the new `default_price_lookup()` factory are the *only* places in
   this package (or, downstream, `src.dashboard`) that construct or
   call `MarketDataService` -- the sanctioned boundary chain is
   Dashboard -> Analytics/Portfolio Valuation -> MarketDataService ->
   Canonical Data, enforced structurally by
   `tests/test_architecture.py`.

4. **`src/dashboard/` -- a read-only Streamlit interface over
   `src.analytics`.** Four pages (`app.py` routes via a sidebar radio;
   `views.py` renders each): Overview (recent experiments' key metrics,
   the most recently run experiment's paper-portfolio summary),
   Backtest/Experiment Analysis (identity/provenance, all metrics,
   equity/drawdown/cumulative-P&L charts, trade P&L distribution, the
   trade table, and the persisted research report if any), Strategy
   Comparison (a side-by-side metrics table plus a bar-aligned equity
   comparison, with `compare_experiments()`'s warnings surfaced
   directly), and Paper Portfolio (cash/equity/unrealized/realized P&L,
   exposure, and the open-position breakdown for one experiment's own
   paper-executed portfolio -- not a new global paper-trading account:
   confirmed via `AskUserQuestion`, the user selecting per-experiment
   portfolio over building the standing "PaperTradingLoop" ROADMAP.md
   already defers as future work). The dashboard never computes a
   metric itself -- every number comes from `AnalyticsService`/
   `PortfolioValuationService`; the one exception is
   `equity_curve - equity_curve.iloc[0]` for the cumulative-P&L chart,
   a presentation-only re-basing of an already-computed series, not a
   new calculation. `formatting.py` is pure, Streamlit-free string
   formatting (rounding/display belongs only here, never in
   `src.analytics`, which preserves full precision). `st.cache_data` is
   used only in `views.py`'s small data-loading functions (a
   presentation-layer optimization, capped to the most recent 20
   experiments on the Overview page to avoid recomputing analytics for
   every experiment ever logged) -- never inside `src.analytics`
   itself. Every unexpected failure in the Paper Portfolio path
   (`_safe_value_portfolio`) is logged via loguru and shown as a
   friendly message, never a raw traceback. Nothing in this package can
   submit an order, change a risk limit, or mutate a
   `Portfolio`/`Experiment`/`Strategy` record -- there is no such method
   anywhere in `src/dashboard`, and a smoke test asserts the underlying
   `Portfolio` is byte-for-byte unchanged after a full render.

**Explicitly not built this round** (per the sprint's own non-goals):
AI/ML signal generation, LLM trading decisions, AI agents or AI
strategy selection (Candidate B, deferred); order placement or broker
execution from the dashboard; a global standing paper-trading account
(`PaperTradingLoop`, already deferred by ADR-0021); real-broker P&L in
the dashboard (Alpaca/IBKR/IG/Tiger are all out of scope this sprint);
execution-realism/slippage/commission simulation as a new subsystem;
tick/order-book data, HFT, or websocket infrastructure; auth/user
management; cloud deployment; a mobile UI; portfolio optimization; new
strategy families.

**Consequences:** Extension Cost (ADR-0014): two new top-level
packages (`src/analytics/`: `models.py`, `metrics.py`, `service.py`,
`valuation.py`; `src/dashboard/`: `app.py`, `views.py`,
`formatting.py`), four new tables and eight new methods on
`ExperimentRegistry`, one new classmethod on `Portfolio`, and
`scripts/run_experiment.py` extended to persist trades/equity/
portfolio/research-report at its existing single write point -- no
existing method signature changed except
`_fill_signals_through_risk_and_execution()`'s new `portfolio`
parameter (internal to that script, not part of any public API). New
tests: `tests/test_analytics.py` (59: `Metric`/`MetricStatus`, every
metric function's hand-calculated edge cases -- empty/one/all-winning/
all-losing/mixed/zero-P&L/zero-variance/flat-curve -- timeframe/
annualization, `AnalyticsService`, `compare_experiments()`),
`tests/test_portfolio_valuation.py` (13: long/short/multiple
positions, missing-price aggregation, realized-P&L independence,
read-only verification, `latest_price()`/`default_price_lookup()`),
`tests/test_dashboard_formatting.py` (16, unconditional -- no
Streamlit dependency to skip on), `tests/test_dashboard_smoke.py` (9,
gated with `pytest.importorskip("streamlit")` and using
`streamlit.testing.v1.AppTest`, fully network-free via a
class-level `MarketDataService.get_history` monkeypatch), 12 added to
`tests/test_experiments.py` and 2 added to
`tests/test_run_experiment_script.py` for the new persistence, and 8
added to `tests/test_architecture.py` for the Sprint 9 boundaries
(analytics does not depend on dashboard or Streamlit; dashboard
depends on analytics; no circular dependency; dashboard never imports
yfinance or reads the cache directly; `MarketDataService` is
constructed only inside `src.analytics.valuation`). Sandbox-verified:
677 passed, same 2 known environment-only artifacts, plus 1 correctly
skipped (`test_dashboard_smoke.py`, no Streamlit installed in this
sandbox -- expected to run for real on the dev machine, where
`streamlit==1.59.2` is already pinned in `requirements.txt`).

**Correction (post-commit cleanup):** the first real `pytest` run on
the dev machine (Python 3.14.6) returned **6 failed, 682 passed** --
every failure inside `tests/test_dashboard_smoke.py`, the one file
this sandbox could never actually execute (only
`pytest.importorskip`-skip it), so this is exactly the class of gap
the project's "sandbox-verified is not real-verified" standing rule
exists to catch.

Root cause: `src/dashboard/views.py`'s `@st.cache_data`-decorated
loaders (`_load_experiment_summaries` and friends) key their cache
purely on the `db_path` argument -- the relative string
`"data/experiments.db"`. Every test in the file uses that identical
string (each pointing at a different `tmp_path` after `chdir`, via
the `workdir` fixture), and the whole file runs in ~3 seconds --
comfortably inside the loaders' 30-60s TTL. Streamlit's cache has no
way to know two calls with the same string argument, made from two
different working directories, mean two different databases, so
`test_app_runs_with_no_experiments` (the first test, genuinely empty)
poisoned the cache for every test that ran after it within the TTL
window: `list_experiment_ids()` kept returning that first test's
stale, empty result no matter what a later test had actually
populated. That routed every later test into `app.py`'s "No
experiments found yet" branch instead of the page it meant to
exercise, which explains all six failures directly: two
`assert ... in h.value for h in at.header` checks failed because the
Overview/Analysis/Paper-Portfolio headers never rendered; the
Comparison page's `IndexError: list index out of range` fired because
its `st.sidebar.multiselect` never rendered (its branch never ran);
the "fewer than two selected" test saw the wrong info message ("No
experiments found" instead of "Select two or more"); and the
price-unavailable test saw zero warnings because the Paper Portfolio
branch, and therefore `_safe_value_portfolio`, was never reached. The
two read-only-invariant tests in the same file (mutation and
no-network-call) still passed throughout, because both assertions
hold regardless of which UI branch actually rendered -- they were
insensitive to the bug, not evidence against it.

This is a test-isolation bug only, not a production one: a real
deployment has exactly one `data/experiments.db`, so no two calls
with that identical string ever mean two different underlying
databases, and the 30-60s cache lag after a fresh
`run_experiment()` call is the intentional, documented trade-off
(section 34 of the Sprint 9 spec) -- unrelated to this failure mode.
No production code changed. Fix: the `workdir` fixture
(`tests/test_dashboard_smoke.py`) now calls `st.cache_data.clear()`
immediately after `chdir`, so every test starts with a cold cache and
can only ever read data it (or a step earlier in the same test)
actually wrote. No `src/dashboard`/`src/analytics` code was touched.

Sandbox-reverified after the fix: 677 passed, same 2 known
environment-only artifacts, `test_dashboard_smoke.py`'s 9 tests still
correctly skipped in-sandbox (no Streamlit installed there -- the fix
itself cannot be sandbox-exercised, only reasoned through and handed
back for a second real run). Real-`pytest` reconfirmation on the dev
machine (expected 688 passed: 682 + the 6 tests this fix should now
turn green) is pending -- this sprint is not yet marked verified.

**Second correction (post-commit cleanup):** the second real `pytest`
run returned **2 failed, 686 passed** -- down from 6, confirming the
cache-clearing fix above resolved four of the six original failures,
but two remained, both again inside `tests/test_dashboard_smoke.py`:

1. `test_app_runs_experiment_analysis_page` failed with
   `RuntimeError: AppTest script run timed out after 3(s)`. Once the
   cache fix let this test actually reach the real Backtest/Experiment
   Analysis page (rather than the trivial "no experiments" branch it
   had been silently hitting before), the page's genuine work --
   computing analytics, drawing three line charts, a bar chart, and two
   dataframes -- took longer than `AppTest`'s default 3-second
   per-`.run()` timeout under a real Streamlit runtime. `AppTest`
   re-executes the entire script from scratch on every `.run()` (it
   doesn't keep a warm server the way real Streamlit does between
   reruns), so 3 seconds is a tight budget for a page this size. Fix:
   every `.run()` call in the file now passes `timeout=15` -- a test-
   tooling accommodation, not evidence of a production performance
   problem, since a real Streamlit server never re-executes cold like
   this between interactions.

2. `test_paper_portfolio_page_degrades_gracefully_when_price_unavailable`
   still saw zero warnings rendered. Unlike the first correction, this
   one's exact mechanism could not be fully confirmed by static
   reading of `src/dashboard/views.py`/`src/analytics/valuation.py`
   alone -- every individual piece (`latest_price()`'s
   `except DataProviderError` correctly catching `NoDataError`,
   `PortfolioValuationService.value()`'s warning-on-`None`-price
   logic) is independently unit-tested and passing, and tracing the
   full call chain by hand does not reveal where a warning would be
   dropped. The one genuine gap identified: `_cached_latest_price`/
   `_value_portfolio` carry a 60s TTL, and this specific test first
   populates its experiment while `MarketDataService.get_history` is
   still the autouse fixture's success stub, then switches it to raise
   `NoDataError` -- a real 60-second cache lag between "price was
   available" and "price just became unavailable" is intentional,
   correct production behavior, but makes this test's outcome depend
   on cache timing it shouldn't have to reason about. Applied, as a
   genuine improvement regardless of whether it is the full
   explanation: an extra `st.cache_data.clear()` immediately after the
   `get_history`-failure monkeypatch, so the test's "price just became
   unavailable" moment can never be served a stale, previously-
   successful price from anywhere. Also widened the assertion to dump
   `at.error`/`at.info`/`at.metric` on failure, so a third occurrence
   (if any) will show conclusively whether `_safe_value_portfolio`'s
   except-Exception branch fired (an `st.error`, meaning a real,
   unidentified exception) or the page rendered cleanly with an
   all-priced snapshot (meaning the price genuinely resolved to a
   non-`None` value, and the cache theory above needs revisiting).

Sandbox-reverified after both changes: 677 passed, unchanged (neither
change touches sandbox-reachable code paths -- `test_dashboard_smoke.py`
is still correctly skipped there). A third real-`pytest` run is
pending; if the price-unavailable test fails again, its enriched
assertion message is the next debugging input, not another guess.

**Third correction (post-commit cleanup) -- the actual root cause.**
The third real `pytest` run returned **1 failed, 687 passed** -- the
timeout fix held, and the enriched assertion added in the second
correction did exactly its job: it printed
`info=['No open positions.'] metrics=[('Cash', '$98,673.47'), ('Equity',
'$98,673.47'), ('Unrealized P&L', '$0.00'), ...]`. The portfolio had
**zero open positions** -- not one, as
`test_paper_portfolio_page_degrades_gracefully_when_price_unavailable`
(and two sibling tests) had assumed from a comment claiming this was
"proven already by `tests/test_run_experiment_script.py`'s sibling
assertions." That claim was checked directly against the actual file
and found false: `test_run_experiment_with_rsi_mean_reversion` asserts
`run_result.result.trades` is non-empty and the spec fields match --
it never asserts anything about `run_result.portfolio.positions` for
this symbol/strategy/params/closes combination. Both of the two
earlier corrections in this entry (the extra cache-clear, the
timeout bump) were real, harmless improvements, but neither was ever
the cause of this specific failure -- the portfolio never had a
priced position to fail to price in the first place, so
`PortfolioValuationService.value()`'s loop never ran and never had a
reason to append a warning. Correct behavior, wrong test premise.

Root cause, confirmed empirically (not just reasoned): `RSI_CLOSES`
(`[100 - i*2 for i in range(15)]` then `[70 + i*3 for i in range(15)]`)
recovers strongly enough in its second half that
`RSIMeanReversionStrategy` (period 5) exits back to FLAT before the
series ends, closing the position it opened during the decline --
matching what the dashboard smoke test's own diagnostic output showed
(zero open positions, a nonzero realized P&L). `src.indicators.
formulas.relative_strength_index` computes RSI via
`ewm(alpha=1/period)` over gains and losses (no rolling-window
warmup -- it's defined from the second bar onward), so a strategy
watching it reacts as soon as the underlying series does anything,
including a strong-enough recovery.

Fix, verified the same way: a new `OPEN_POSITION_CLOSES` constant
(`tests/test_dashboard_smoke.py`) -- just the declining half,
`[100 - i*2 for i in range(15)]`, with no recovery leg at all. Since
every delta is a loss and none is a gain, `avg_gain` is pinned at
exactly `0.0` by the `ewm` formula from the second bar onward, so RSI
reads exactly `0` for the rest of the series -- deeply oversold,
never overbought, so `RSIMeanReversionStrategy` enters LONG at the
second bar and can never emit an exit signal (there is no up-day to
ever push RSI back to `overbought`). Running the real
`run_experiment()` pipeline directly against this series (symbol
`QQQ`, `rsi_mean_reversion`, `period=5`) confirms: 1 trade, 1 open
position (`QQQ`, entry price `98.0`, quantity `~102.04`), 0 closed
positions -- exactly the fixture every affected test actually needed.
The three tests that specifically need an open position now use
`OPEN_POSITION_CLOSES` instead of `RSI_CLOSES`, each gained a sanity
check asserting `len(portfolio.positions) == 1` before the `AppTest`
even runs (so a future regression here fails at the assertion closest
to the actual cause, not several layers downstream in a Streamlit
render), and `test_app_runs_paper_portfolio_page_with_an_open_position`
gained a real assertion that the per-position dataframe
(`render_paper_portfolio`'s `if snapshot.positions:` branch) actually
renders with `"QQQ"` in it -- a code path this file had never
genuinely exercised before, since every "open position" fixture
before this fix was, in fact, closed by the end of its own run.
`RSI_CLOSES` itself is untouched and still used where an open position
isn't required (the Comparison-page tests, which only need a second,
differently-configured experiment to compare against).

Sandbox-reverified: 677 passed, unchanged. **Confirmed via real
`pytest` on the dev machine: 688 passed, 0 failed, all green** -- the
fourth real run, and the first one this sprint's own test suite
actually passed outright. Sprint 9 (Analytics & Dashboard) is now
genuinely verified, not just implemented or sandbox-verified.

## ADR-0043: Sprint 10 -- ML Signal Research & AI Strategy Integration

**Status:** Implemented and confirmed via real `pytest` on the dev
machine: **784 passed, 0 failed, all green** -- see "Sandbox
verification" below for the two-round path to that result.

**Context:** `ROADMAP.md` named Sprint 10 ("AI") as explicit future
work when Sprint 9 resolved in favor of Analytics & Dashboard, and left
the approach itself an open choice between classic ML on engineered
features and LLM-based reasoning over market context. This round
chooses classic ML -- deterministic, lightweight, interpretable,
fast to test -- and defers LLM-based trading decisions entirely (the
existing AI Research Reporter's LLM/fallback narrative path,
`src/research/`, is a different capability and is untouched). The
platform's standing architectural principle (ADR-0000: every new
component is an extension, not a rewrite) sets the one hard constraint
this sprint has to satisfy: **AI is another signal source, not a
replacement for strategy, risk, execution, portfolio, or backtesting.**
A model that required changing `Backtester`, `PortfolioRiskEngine`, or
`PaperBroker` to work would be evidence the AI layer was placed in the
wrong layer, not a reason to change those.

**Decision:**

1. **`src/ai/` -- research capability only, never a decision-maker.**
   Nine small modules, each with one job: `features.py`
   (`FeatureBuilder`/`FeatureSpec` -- 8 past-only columns: 1/5/20-bar
   return, `(EMA12-EMA26)/close`, `RSI(14)`, `ATR(14)/close`, MACD
   histogram normalized by close, `volume / rolling(20) mean`, all
   built on `src.indicators.IndicatorEngine`, never a reimplemented
   formula); `labels.py` (`LabelBuilder`/`LabelSpec` -- 3-class
   forward-return target, `LONG`/`SHORT`/`FLAT`, configurable
   `horizon_bars`/`neutral_threshold`, deliberately a separate class
   from `FeatureBuilder` since a feature describes information at `t`
   and a label describes an outcome after `t`); `dataset.py`
   (`build_training_table()` -- the one place features and labels are
   combined and warmup/horizon-undefined rows are dropped);
   `splitting.py` (`chronological_split()` with a `purge_bars` embargo,
   `walk_forward_splits()` for expanding-window evaluation -- see
   decision 3); `model.py` (`AIModel` interface,
   `LogisticRegressionModel`, a `register_model_type()`/`build_model()`
   registry mirroring `src.strategies.registry`'s own pattern);
   `identity.py` (`model_spec_id()`); `artifacts.py`
   (`ModelArtifactStore`, joblib + SHA-256); `registry.py`
   (`ModelMetadata`/`ModelRegistry`); `evaluation.py`
   (`classification_metrics()`); `training.py`
   (`MLTrainingService`/`TrainingResult`/`WalkForwardFoldResult`). None
   of these nine files imports `src.broker`, `src.execution`,
   `src.risk`, `src.portfolio`, or `src.dashboard` -- enforced
   structurally by `tests/test_architecture.py`, not just by
   convention. `src/ai/` produces knowledge (a trained classifier, its
   evaluation metrics, its provenance) for the next layer to consume;
   it never sizes a position, places an order, or touches account
   state.

2. **Classic ML, one model, scikit-learn only.** `LogisticRegressionModel`
   wraps `sklearn.pipeline.Pipeline(StandardScaler, LogisticRegression)`
   -- no PyTorch/TensorFlow/XGBoost/LightGBM/LLM API added merely
   because it might be useful later (Sprint 10 spec, section 3). The
   scaler is fit exactly once per model, inside `Pipeline.fit()`, on
   whatever `X`/`y` `AIModel.fit()` receives -- `MLTrainingService` is
   the only caller, and it only ever passes the training split;
   `tests/test_ai_model.py`'s
   `test_scaler_is_fit_only_on_the_data_passed_to_fit_not_refit_on_predict`
   pins this structurally (predicting against a wildly different-scaled
   `X` must not change the fitted pipeline's own learned scaling).
   `AIModel.predict()`/`predict_proba()` reject a mismatched feature
   schema (missing column, reordered column, wrong name) with a loud
   `ValueError` rather than a silently wrong prediction (Sprint 10
   spec, section 60) -- the same "fail loud, never fabricate" posture
   `src.analytics.models.Metric` already established for undefined
   numbers.

3. **Leakage prevention is structural, not a convention to remember.**
   `chronological_split()` never shuffles; it splits a `pd.Index` by
   position and drops `purge_bars` rows off the end of train and of
   validation, where `purge_bars` is always the label's own
   `horizon_bars` -- a label at row `t` encodes `close[t+horizon]`, so a
   training row within `horizon_bars` of a boundary has an outcome
   window reaching into the next split; purging removes exactly that
   overlap. `walk_forward_splits()` applies the same embargo per fold
   in an expanding-window evaluator (Sprint 10 spec, section 12) --
   diagnostic only, never persisting an artifact, and explicitly not a
   hyperparameter optimizer. `tests/test_ai_splitting.py` proves the
   embargo arithmetic directly (`last_train_position + horizon_bars <
   first_validation_position`), not just that the code runs.
   `tests/test_ai_labels.py`'s leakage regression test is the sharpest
   version of this guarantee: two datasets identical through row `t`,
   differing only in a close far beyond `t + horizon`, must produce
   **identical** feature rows through `t` (proven with
   `pd.testing.assert_frame_equal`) while their labels at the affected
   row are free to differ.

4. **Model identity is a specification hash, never a name.** Two
   models both called `"logistic_regression"` can differ in
   hyperparameters, feature configuration, label configuration, or
   training data -- `model_spec_id()` (`src/ai/identity.py`) hashes all
   of those together (model type, hyperparameters, `feature_set_id`,
   `label_spec_id`, the training dataset's own `content_hash` --
   Sprint 8's dataset identity, not a hash of the derived feature
   matrix -- plus the training range and random state) into one
   deterministic id. `FeatureBuilder.feature_set_id()` and
   `LabelBuilder.label_spec_id()` are the same pattern one level down:
   a 5-bar-horizon model and a 20-bar-horizon model, or a
   14-period-RSI feature set and a 21-period one, never collide under
   an ambiguous shared name (`tests/test_ai_training.py`,
   `test_different_label_horizon_produces_a_different_model_id`).
   Deliberately distinct from the **artifact hash**
   (`ModelArtifactStore.save()`'s SHA-256 of the actual persisted
   joblib bytes) -- the spec id says what the model is supposed to be,
   the artifact hash says exactly which trained object was persisted.

5. **Model persistence: filesystem, not a second SQLite schema.**
   `ModelArtifactStore` saves one `joblib`-serialized artifact per
   `model_id` under `data/models/{model_id}/model.joblib`;
   `ModelRegistry` saves one `metadata.json` alongside it. Chosen over
   extending `ExperimentRegistry`'s SQLite schema (Sprint 10 spec,
   section 18 explicitly warns against "an unnecessarily large database
   abstraction" for what is, this sprint, a handful of models) --
   `list_models()`/`get_metadata()`/`load_model()` give the same
   query surface `ExperimentRegistry` gives for experiments, at a
   fraction of the mechanism. `ModelRegistry.load_model()` only ever
   reads from this platform-controlled directory, keyed by a `model_id`
   the platform itself generated -- there is no method anywhere that
   accepts an arbitrary filesystem path for deserialization (`joblib`/
   `pickle` can execute code on load; the trust boundary is "this
   platform's own training pipeline wrote it," never "a caller-supplied
   path," Sprint 10 spec section 61). `data/models/` needs no
   `.gitignore` change -- `data/*` is already ignored (the same
   `data/experiments.db` precedent, ADR-0016) -- so model binaries are
   runtime-generated state, never committed.

6. **`AISignalStrategy` -- the entire seam between `src.ai` and the
   rest of the platform.** `src/strategies/ai_signal.py` loads an
   already-trained, frozen model by `model_id` at construction
   (`ModelRegistry.load_model()`) and never calls `.fit()` again. Its
   `params` property returns exactly `{"model_id", "min_probability"}`
   -- enough for `ExperimentSpec.reconstruct_strategy()`
   (`src.experiments.spec`, unchanged since Sprint 6) to rebuild an
   equivalent instance from those two values alone, with no mutable
   external default in the way (`ModelRegistry()`'s default directory
   is a fixed constant, the same posture `ExperimentRegistry`'s
   `DEFAULT_DB_PATH` already takes). Model output maps onto the
   existing `Signal`/`SignalDirection` -- there is no second,
   AI-specific decision model. Predictions below `min_probability` are
   treated as `FLAT`/no-change regardless of which class the model
   favored (confidence is never translated into position size -- that
   stays `src.risk`'s job entirely, untouched this sprint), and signals
   are emitted sparsely -- only when the *effective*, post-threshold
   direction changes (ADR-0015's existing sparse-signal convention,
   proven by `tests/test_ai_signal_strategy.py`,
   `test_signals_are_sparse_not_one_per_valid_row`) -- never one Signal
   per candle merely because the model has an opinion on every row.
   Signal metadata carries `model_id`/`model_artifact_hash`/
   `predicted_class`/`class_probability`/`feature_set_id`/
   `label_horizon_bars` -- enough evidence to trace a signal back to
   its exact model without dumping the full feature vector into every
   row (Sprint 10 spec, section 29).

7. **No `ExperimentSpec`/`ExperimentRegistry` changes needed.**
   Investigated first, per this sprint's own "extend only where
   necessary" instruction: `ExperimentSpec.capture()` already reads
   `strategy.params` generically, so `AISignalStrategy`'s `model_id`/
   `min_probability` flow through the *existing* `strategy_params`
   field with zero changes to `ExperimentSpec`, `ExperimentRegistry`,
   or their schemas. An AI-backed experiment answers Sprint 10 spec
   section 31's provenance questions by composition, not by a second
   lineage system: "which ML model / which artifact / which feature
   set / which label definition / which training dataset+range" all
   live in `ModelRegistry`, looked up by the `model_id` already
   sitting in the experiment's own recorded `strategy_params`.

8. **Existing pipeline stays exactly as generic as it was.**
   `Backtester` gained no AI-specific branch (verified structurally by
   `tests/test_architecture.py`,
   `test_backtester_has_no_ai_specific_branch` -- a literal source-text
   scan for `"ai_signal"`/`"AISignalStrategy"`/`"src.ai"`);
   `EMACrossStrategy`/`RSIMeanReversionStrategy` are untouched;
   `src.risk`/`src.execution`/`src.portfolio`/`src.dashboard`/
   `src.analytics` gained no dependency on `src.ai` in either direction.
   `AnalyticsService`/`Backtester` are what answer "how did the
   resulting trading policy perform" -- `src.ai.evaluation`'s
   classification metrics (accuracy, balanced accuracy, precision/
   recall, confusion matrix, log loss, class-distribution/signal-
   coverage diagnostics) answer the different question "how well did
   the model classify outcomes," and the two are never conflated into
   one number (Sprint 10 spec, sections 22, 58, 67).

**Non-goals, explicit:** no LLM in the AI signal layer (OpenAI,
Anthropic, Gemini, local LLM, prompt-based trading -- all explicitly
out; the AI Research Reporter's own separate LLM path is unaffected
and unchanged); no model zoo (`LogisticRegression` is the only
production model this sprint; the `register_model_type()` seam exists
so a second implementation is "implement it, register it," not a
rewrite, but Sprint 10 ships exactly one); no hyperparameter optimizer;
no automatic "best model" selection or composite AI score; no live
inference daemon, continuous retraining, or autonomous trading; no new
AI-specific dashboard page (the existing Overview/Analysis/Comparison/
Paper Portfolio pages are untouched).

**Sandbox verification:** scikit-learn (and `joblib`) are real
`requirements.txt`-pinned dependencies, exactly like Streamlit was for
Sprint 9 (ADR-0042) -- and, exactly like Streamlit, neither is
installed in this sandbox, which has no network access to install
them. The same established pattern applies: every test that needs a
real model fit is gated with `pytest.importorskip("sklearn")` (and
`"joblib"` where relevant) at module level, so it skips cleanly here
and runs for real on the dev machine, rather than either crashing the
suite or being stubbed into something that no longer tests the real
library. Everything that doesn't need a fitted model --
`tests/test_ai_features.py` (10), `tests/test_ai_labels.py` (11),
`tests/test_ai_dataset.py` (7), `tests/test_ai_splitting.py` (15), plus
6 new boundary assertions in `tests/test_architecture.py` (4 run for
real in-sandbox, 2 gated the same way) -- passed in the sandbox's own
stub-based runner: **725 passed, 2 failed (the same pre-existing
cli/doctor and config environment-only failures every prior sprint has
carried forward, unrelated to this change), 8 skipped** (6 whole
modules gated on scikit-learn/joblib, plus the 2 gated assertions
inside `test_architecture.py`). The gated modules --
`tests/test_ai_model.py`, `tests/test_ai_registry.py`,
`tests/test_ai_training.py`, `tests/test_ai_signal_strategy.py`,
`tests/test_ai_end_to_end.py` -- had been reviewed carefully against
the real scikit-learn API, but per this platform's own standing
process were not claimed "verified" until a real `pytest` run
confirmed them.

Real `pytest` on the dev machine, round 1: **782 passed, 2 failed**.
Both failures were bugs in the *tests themselves*, not in `src/ai` or
`src/strategies/ai_signal.py` -- exactly the kind of thing sandbox
review, without a real scikit-learn to run against, could not have
caught:

- `test_repeated_backtests_against_the_same_frozen_model_are_identical`
  asserted `result_a.trades == result_b.trades` on full `Trade`
  dataclasses, which include `entry_signal_id`/`exit_signal_id`.
  `Signal.id` is `field(default_factory=uuid4)` (`src/signals/models.py`)
  -- an existing, deliberate design from long before this sprint, not
  something Sprint 10 introduced. Two independent calls to
  `AISignalStrategy.generate_signals()` against the same frozen model
  and the same candles produce identical directions, timestamps, and
  probabilities (confirmed directly in the same test), but each call
  constructs fresh `Signal` objects with new random UUIDs -- so full
  dataclass equality was guaranteed to fail regardless of how
  deterministic the model was. Fixed by comparing trade economics
  (`entry_time`, `exit_time`, `direction`, `entry_price`, `exit_price`)
  instead of raw dataclass identity -- this is what "the same trades"
  actually means for a backtest-integrity check; the signal ids were
  never part of that claim.
- `test_ai_signal_strategy_emits_canonical_signal_objects`'s guard
  against a parallel "AISignal" domain model used
  `assert "class AISignal" not in text` -- a plain substring check that
  also matches inside the real, intended `class AISignalStrategy`,
  making it a guaranteed false positive on the very code it was meant
  to pass. Fixed with a word-boundary regex,
  `re.search(r"class AISignal\b(?!Strategy)", text) is None`, which
  still catches a genuine rogue `AISignal` class while letting
  `AISignalStrategy` through.

Real `pytest` on the dev machine, round 2 (after both fixes):
**784 passed, 0 failed, all green.** Notably, the sandbox's 2 "known
environment-only" failures (the cli/doctor Python-version check and a
`test_config` failure, carried forward unchanged since Sprint 7) don't
reproduce on the dev machine at all, since it runs its own genuinely
supported Python version -- so this confirmation run has zero failures
of any kind, not just zero *new* ones. See `PROJECT_STATE.md` and
`CHANGELOG.md` for the same account.

## ADR-0044: Sprint 11 -- Portfolio-Aware Backtesting & Unified Risk Simulation

**Status:** Accepted. Confirmed via real `pytest` on the dev machine:
850 passed, 0 failed.

**Context:**

Since ADR-0011, `Backtester.run()` has sized every trade as exactly
one unit -- a deliberate simplification so the framework could focus
on turning a sparse `Signal` list into trades, a position series, and
an equity curve, without also solving position sizing. That
simplification has held for ten sprints, through the introduction of
timeframe-agnosticism (ADR-0038), the AI strategy path (ADR-0043), and
the entire Sprint 7 portfolio-risk architecture
(`PortfolioRiskEngine`/`RiskDecision`/`PortfolioRiskLimits`/`RiskLimits`
/`Portfolio`/`Position`/`ApprovedTradeIntent`), which today governs
every trade `PaperBroker` places -- but has never been asked to govern
a single trade inside `Backtester`.

The result is a real gap: a strategy's backtested performance and its
paper-traded performance are shaped by two unrelated sizing models.
Backtest equity curves say nothing about what would happen once
concurrent-position limits, portfolio exposure caps, or per-trade risk
budgets are applied -- because they never are. A strategy can look
excellent unit-sized and behave completely differently once real
position sizing and portfolio admission rules are in the loop. This
sprint closes that gap: the same risk model that already constrains
paper trading can now constrain historical research too.

This is explicitly a risk-*consistency* sprint, not an execution-
realism sprint. Slippage, transaction costs, partial fills, and
realistic order-book modeling remain future work for `src/execution/`
-- untouched here, same as every prior sprint.

**Decision:**

1. **Two explicit, typed execution modes, never silently mixed.**
   `RiskMode(str, Enum)` -- `LEGACY_UNIT` and `PORTFOLIO_RISK` -- is
   carried on a new `BacktestConfig` (`src/backtesting/config.py`) and
   echoed back on `BacktestResult.risk_mode`, so any consumer (the
   dashboard, `ExperimentRegistry`, a human reading a report) can tell
   at a glance which sizing model produced a given result.
   `Backtester.run()` -- the original ADR-0011 method -- is completely
   unchanged and always reports `LEGACY_UNIT`; the new
   `Backtester.run_portfolio()` always reports `PORTFOLIO_RISK`.
   Neither method shares state with the other, and both remain
   available side by side indefinitely -- see non-goal 1 below on why
   legacy mode isn't being deprecated.

2. **`PortfolioBacktestEngine` composes Sprint 7's risk engine; it
   never reimplements it.** The new module
   `src/backtesting/portfolio_engine.py` runs each symbol's strategy,
   merges the resulting sparse signals onto one sorted, multi-symbol
   timeline, and for each signal in chronological order: looks up the
   signal-bar close as the entry price, asks a `StopPolicy` for a stop,
   and hands both to the *existing*, *unmodified*
   `PortfolioRiskEngine.decide()` (for entries) or `.decide_close()`
   (for exits/FLATs) against a real, evolving `Portfolio` object --
   never a stale snapshot, never a synthetic replica. `risk_amount`,
   `risk_quantity`, `capital_quantity`, `allocation_quantity`,
   `portfolio_exposure_quantity`, and `symbol_exposure_quantity` are
   computed exactly once, inside `PortfolioRiskEngine`, and nowhere
   else; `test_architecture.py`'s
   `test_portfolio_backtest_engine_does_not_duplicate_risk_sizing_arithmetic`
   enforces this by scanning `portfolio_engine.py`'s source text for
   any local assignment to those names. The resulting `RiskDecision` is
   wrapped in an `ApprovedTradeIntent` and filled at the signal bar's
   close (the same fill convention `run()` already uses), then applied
   to the `Portfolio` via its existing `open_position()`/
   `close_position()` methods -- no second `Position` model, no second
   P&L engine, no second copy of anything Sprint 7 already owns.

3. **A new `StopPolicy` abstraction, not a new `Signal` field.**
   `PortfolioRiskEngine.decide()` requires a stop price to size a
   trade, but strategies deliberately have no concept of one --
   `Signal` stays exactly as small as ADR-0015 left it, with no
   quantity or stop field ever added to it (Sprint 11 spec, sections 5
   and 24: strategies never control sizing, and never will).
   `src/backtesting/stop_policy.py` introduces a `StopPolicy` protocol
   and one concrete implementation, `ATRStopPolicy` (period=14,
   multiple=2.0 by default), built on the existing `IndicatorEngine` --
   LONG stops below entry, SHORT stops above entry, by
   `multiple * ATR`. `PortfolioBacktestEngine` always truncates each
   symbol's candle history to `.loc[:signal.timestamp]` before handing
   it to the stop policy, so a stop can never be computed from data the
   strategy itself couldn't have seen yet
   (`test_stop_price_is_unaffected_by_data_after_the_signal_timestamp`
   in `tests/test_stop_policy.py` regresses this directly). When ATR
   can't be computed yet (insufficient warmup history) or comes back
   zero, the policy returns an explicit `StopResult(available=False,
   reason=...)` rather than a bad number, and `PortfolioBacktestEngine`
   turns that into an explicit `STOP_UNAVAILABLE` rejection -- it never
   falls back to unit quantity or any other silent default.

4. **`Trade` gains a persistent, optional `quantity` field; P&L becomes
   real dollars when it's known.** `Trade.quantity: float | None`
   defaults to `None`, so every `LEGACY_UNIT` trade (past and future)
   is unaffected. When `quantity` is set, `Trade.gross_pnl` reflects
   it directly -- `qty * (exit - entry)` for longs, `qty * (entry -
   exit)` for shorts -- as gross realized P&L before costs. Analytics
   (`src/analytics/metrics.py`) gates a full family of new dollar
   metrics (`net_pnl_dollars`, `gross_profit_dollars`,
   `expectancy_dollars`, `average_winner_dollars`, and so on) behind a
   new `has_quantity_detail(trades)` check, returning an explicit
   `Metric.undefined(...)` rather than silently reinterpreting an
   unsized trade list as dollar-denominated. The original fraction-
   based metrics are untouched and still apply to every trade list
   regardless of mode.

5. **Persistence needed almost no schema change.** `Portfolio`/
   `Position` serialization in `src/experiments/registry.py`
   (`_position_to_dict`/`_dict_to_position`) has round-tripped
   `quantity`/`side`/`stop_price` generically since Sprint 9 -- zero
   change. `ExperimentSpec.backtest_config` has been a reserved,
   extensible JSON field since before this sprint -- zero schema
   change. The only actual schema change is one new nullable
   `quantity REAL` column on `experiment_trades`
   (`_TRADES_SCHEMA`), following the same precedent ADR-0033 set for
   the `symbol` column; `_row_to_trade()` checks
   `"quantity" in row.keys()` so a pre-migration on-disk database
   (missing the column entirely) degrades gracefully to `None` instead
   of raising, while a present-but-`NULL` value round-trips as `None`
   normally.

6. **AI strategies get zero special-casing, by construction and by
   test.** `run_portfolio()` takes a `dict[str, Strategy]` and treats
   every entry identically -- `EMACrossStrategy`, `RSIMeanReversionStrategy`,
   and `AISignalStrategy` all traverse the exact same signal-generation,
   stop-policy, and risk-decision path, with no `isinstance` branch
   anywhere in `portfolio_engine.py` or `engine.py`. This isn't just
   asserted in prose: `test_architecture.py`'s
   `test_portfolio_backtest_engine_has_no_ai_specific_branch` does a
   literal source-text scan of `portfolio_engine.py` for any mention of
   `AISignalStrategy`, `ai_signal`, or `src.ai`, and
   `tests/test_ai_end_to_end.py` proves it empirically by running a
   trained `AISignalStrategy` through `run_portfolio()` end to end and
   confirming every resulting trade is properly sized and priced. (A
   docstring in an earlier draft of this sprint's code named
   `AISignalStrategy`/`EMACrossStrategy`/`RSIMeanReversionStrategy`
   illustratively and tripped exactly this guard -- see
   `PROJECT_STATE.md`'s Sprint 11 notes and the sandbox verification
   section below; it was reworded to describe the same guarantee
   generically.)

7. **Sparse-signal and reversal semantics are preserved exactly, not
   reinvented.** A reversal or unsupported same-symbol operation while
   a position is already open is rejected via the existing
   `POSITION_SCALING_NOT_SUPPORTED`/`UNSUPPORTED_POSITION_OPERATION`
   rejection reasons `PortfolioRiskEngine` already defines -- no new
   reversal-handling logic was written for this sprint. A close/FLAT
   signal always routes through `decide_close()`, which by design is
   never subject to exposure-limit rejection (closing a position can
   only reduce exposure).

8. **Mark-to-market equity is computed by a read-only valuation pass,
   mirroring ADR's Sprint 9 pattern.** Each symbol's close series is
   forward-filled across one merged, sorted multi-symbol timeline, and
   equity at each point is `cash + sum(open position value)` --
   computed without ever mutating `Position.current_price` or any
   other field on the live `Portfolio`. A defensive fallback to
   `position.valuation_price` exists for a forward-filled price that
   somehow comes back `NaN`, though no test run has ever actually
   triggered it in a coherent scenario.

9. **Every outcome is auditable, never silently dropped.**
   `PortfolioBacktestEngine` records one `SignalOutcome` per signal --
   `accepted: bool`, the `RiskDecision` (or `None` when a stop was
   unavailable, in which case `rejection_reason` reports
   `"STOP_UNAVAILABLE"` without ever reaching the risk engine), and the
   originating signal and symbol -- and `summarize_outcomes()` rolls
   these into a `RiskAuditSummary` with totals and rejection counts by
   reason. Nothing is hidden: "signal generated," "trade approved," and
   "trade rejected" are always distinguishable from the result alone.

**Non-goals, explicit (Sprint 11 spec, sections 3 and 30):**

- Deprecating or removing `Backtester.run()` / `LEGACY_UNIT` mode. It
  stays, indefinitely, as the simple unit-sized baseline it has always
  been -- some research questions genuinely don't need portfolio
  sizing, and forcing every caller through `PORTFOLIO_RISK` would be a
  breaking, not additive, change.
- Any execution-realism modeling: slippage, transaction costs, partial
  fills, realistic order-book behavior. Still `src/execution/`'s job,
  untouched.
- Advanced portfolio management of any kind: Kelly sizing, VaR/CVaR,
  correlation-aware optimization, factor models, volatility targeting,
  dynamic hedging. `PortfolioRiskEngine`'s existing sizing rules are
  used exactly as they are; none of its formulas were touched or
  extended.
- Position-management expansion: pyramiding, partial closes, scaling
  into or out of a position, multi-leg trades. A same-symbol operation
  while already open is rejected, not supported, exactly as Sprint 7
  already specified.
- Live trading of any kind. `PortfolioBacktestEngine` only ever
  operates on a fresh, isolated, in-memory `Portfolio` built for one
  simulation run -- never the live/paper `Portfolio` `PaperBroker`
  manages, and never anything that talks to a real broker.
  `PaperBroker` itself was not modified in any way this sprint, and its
  existing tests pass unchanged.
- Any AI/ML expansion: LLM-based trading, neural signal models,
  automatic model selection, autonomous retraining, AI-driven risk
  models. `AISignalStrategy` (ADR-0043) is used only as one more
  `Strategy` implementation proving decision 6 above; nothing about
  how it trains or predicts was touched.
- New market-data handling of any kind. `Backtester` continues
  accepting only canonical, already-validated `candles` -- it still
  never imports `yfinance`, reads a data cache directly, normalizes
  provider timestamps, or implements a session calendar.

**Sandbox verification:**

The sandbox this work was written in has no network access (`pip
install` fails with a proxy `403 Forbidden`), so `sklearn`, `joblib`,
and `streamlit` remain uninstalled here, same as every prior sprint --
the new AI end-to-end portfolio test
(`test_ai_strategy_runs_through_the_portfolio_aware_backtester`) is
gated behind `pytest.importorskip("sklearn")` and could only be
verified via `py_compile`, not execution. Using the sandbox's stub
test runner (`/tmp/runner_all.py`, the same harness prior sprints'
ADRs describe), the full suite reports:

**790 passed, 2 failed, 8 skipped.**

The 2 failures are the same environment-only failures every sprint
since Sprint 7 has carried forward unchanged --
`test_cli_doctor.test_python_version_passes_against_running_interpreter`
(the sandbox's Python 3.10.12 fails whatever minimum-version check
`src/cli/checks.py` enforces) and
`test_config.test_logger_is_importable_and_callable` (a loguru-stub
gap) -- confirmed via `git status`/`git log` to be untouched by any
change in this sprint. The 8 skips are the expected
`sklearn`/`joblib`/`streamlit` import guards.

**Real `pytest` on the dev machine confirms it: 850 passed, 0 failed**
(Python 3.14.6, pytest 9.1.1, full suite in 4.57s, no skips). Neither
sandbox-only failure reproduced, and every scikit-learn/joblib/
Streamlit-gated test the sandbox could only skip -- including the AI
end-to-end portfolio-backtest integration test this sprint added --
ran for real and passed. Sprint 11 is fully confirmed.

---

## ADR-0045: Sprint 12 -- Execution Realism & Transaction Cost Modeling

**Status:** Accepted. Confirmed via real `pytest` on the dev machine:
916 passed, 0 failed, 3.92s.

**Context:**

ADR-0044 closed the gap between backtested and paper-traded position
*sizing* -- the same `PortfolioRiskEngine` now governs both. It
explicitly deferred execution realism as a non-goal: every
`run_portfolio()` fill has still been an instant, frictionless
transaction at the signal bar's own close, with no slippage, no fees,
and no timing gap between "signal generated" and "trade filled." That
is the last major hidden assumption left in the research chain: a
strategy's backtested equity curve says nothing about what a real fill
would actually cost, or whether tomorrow's price is even knowable at
the moment a today's-bar signal fires.

This sprint replaces that hidden assumption with an explicit,
deterministic execution model: `Signal -> Approved Trade Intent ->
Execution Model -> Reference Execution Price -> Slippage -> Fees ->
Fill -> Portfolio -> Analytics`. It is narrowly scoped to candle-level
research friction -- timing, slippage, and fees -- not an exchange
simulator: no order book, no partial fills, no limit/stop orders, no
broker-specific fee schedules.

**Decision:**

1. **A new `ExecutionModel` in `src/backtesting/`, reusing the existing
   `Fill`/`Order`/`OrderSide` shapes -- never a second fill domain
   model.** `src/backtesting/execution_model.py` introduces
   `ExecutionModel.simulate(order, candles) -> ExecutionOutcome`, a
   pure function of its inputs (no global state, no randomness, no
   wall-clock reads -- enforced by
   `test_execution_model_has_no_hidden_random_state_or_system_time`).
   `src/execution/models.py`'s `Fill` dataclass gained four new,
   defaulted fields (`reference_price`, `fill_timestamp`,
   `slippage_amount`, `fee`) rather than a parallel type -- every
   existing `PaperBroker` call site (`Fill(order=..., fill_price=...,
   cash_delta=...)`) is untouched and still constructs a valid `Fill`.

2. **Two explicit, typed timing conventions -- `SIGNAL_BAR_CLOSE`
   (default) and `NEXT_BAR_OPEN` -- never silently mixed.**
   `ExecutionTiming(str, Enum)` is carried on `ExecutionConfig.timing`.
   `SIGNAL_BAR_CLOSE` reproduces exactly what `run_portfolio()` always
   did before this sprint (the signal's own bar close is both the
   reference and, under zero slippage, the fill price) -- this is the
   default specifically so no existing caller or historical experiment
   is silently reinterpreted (Sprint 12 spec, sections 33, 56).
   `NEXT_BAR_OPEN` is the new, realistic mode: the reference price is
   the *next* candle's `open`, and the fill timestamp is that next
   candle's own timestamp -- never the signal bar's. A researcher must
   opt into `NEXT_BAR_OPEN` explicitly; nothing upgrades a config to it
   automatically.

3. **No-look-ahead is structural, not just tested.**
   `ExecutionModel._reference_price()` only ever reads two cells under
   `NEXT_BAR_OPEN`: `candles.index[position + 1]` (the next bar's own
   timestamp) and that same row's `open` column. It has no code path
   that reads a future bar's `high`/`low`/`close` at all -- the
   look-ahead-protection regression test
   (`test_favorable_future_high_low_close_never_improves_the_fill`)
   proves this empirically by making the next bar's high/low/close
   absurdly favorable while holding its `open` fixed, and showing the
   simulated fill is unchanged.

4. **No next bar, or the signal's own timestamp isn't in the candle
   index, is an explicit outcome, never a silent fallback.** Both
   cases return `ExecutionOutcome(filled=False, reason=NO_EXECUTION_BAR)`
   -- never a same-bar-close fallback (which would reintroduce
   look-ahead ambiguity for `NEXT_BAR_OPEN`) and never a fabricated
   "next calendar timestamp" (which would be wrong across a session
   boundary; see decision 8). `ExecutionOutcome` itself validates this
   invariant in `__post_init__`: a `filled=True` outcome must carry a
   `Fill`, and a `filled=False` outcome must carry a `reason`.

5. **Deterministic, direction-correct slippage and fee models, both
   swappable via a `Protocol`.** `PercentageSlippageModel(slippage_bps)`
   -- `fill_price = reference_price * (1 + bps/10_000)` for a `BUY`,
   `* (1 - bps/10_000)` for a `SELL`: two different formulas per side,
   never one formula with a sign flip that could accidentally apply the
   same direction to both (Sprint 12 spec, section 11; regression-
   tested directly). `slippage_bps=0.0` is the default and produces a
   fill exactly equal to the reference price -- no hardcoded nonzero
   minimum anywhere. `PercentageFeeModel(fee_bps, fixed_fee)` computes
   `notional * (fee_bps/10_000) + fixed_fee`, applied identically
   regardless of side unless a caller configures it otherwise. Neither
   model uses `random`, wall-clock time, or any global state --
   identical inputs always produce identical output
   (`test_slippage_is_deterministic_across_repeated_simulation`,
   `test_simulate_is_pure_same_inputs_same_output`).

6. **Costs enter the economic pipeline exactly once, at the `Fill`.**
   `ExecutionModel.simulate()` computes `fill_price`, `fee`, and
   `cash_delta` (`-notional - fee` for a `BUY`, `notional - fee` for a
   `SELL`) together, in one place. `PortfolioBacktestEngine` applies
   that same `fill.fill_price`/`fill.fee` to `Portfolio.open_position()`/
   `close_position()` (both of which gained an optional `fee: float =
   0.0` parameter, validated non-negative, debited as a cash movement
   additional to the existing `quantity * price` one) and to the
   resulting `Trade`'s new `entry_fill_price`/`exit_fill_price`/
   `entry_fee`/`exit_fee` fields -- never recomputed independently by
   `Portfolio`, `Trade`, or `Analytics`.

7. **`Trade.entry_price`/`exit_price` stay the frictionless *reference*
   price; the actual transacted economics live in new, separate
   fields.** This is the single most important backward-compatibility
   decision this sprint makes: `Trade.gross_pnl` is still computed from
   `entry_price`/`exit_price` exactly as ADR-0044 left it, so under the
   default zero-cost `ExecutionConfig` (where `entry_fill_price ==
   entry_price` and `exit_fill_price == exit_price` exactly),
   `gross_pnl` and the real fill-based P&L agree to the cent -- no
   pre-Sprint-12 trade's numbers change. Three new properties make the
   cost-aware picture explicit without touching that: `total_fees`
   (`entry_fee + exit_fee`, always defined), `slippage_cost` (`gross_pnl`
   minus the P&L the actual fill prices would produce -- `None` only
   when a fill price is missing, and provably `>= 0` for any
   non-negative configured slippage regardless of LONG/SHORT direction),
   and `net_pnl` (`gross_pnl - (slippage_cost or 0.0) - total_fees`,
   `None` only when `gross_pnl` itself is, i.e. a `LEGACY_UNIT` trade).
   `entry_price`/`exit_price` are never mutated to "incorporate" a cost
   (Sprint 12 spec, section 43).

8. **Session boundaries fall out of the canonical candle index for
   free -- no calendar-specific code was written.** `NEXT_BAR_OPEN`'s
   "next bar" is `candles.index[position + 1]`, whatever that next row
   actually is; across a Friday-to-Monday gap in a business-day-indexed
   dataset, that is genuinely Monday's row, never a fabricated Saturday
   (`test_next_bar_open_across_a_weekend_uses_the_next_trading_session`).
   `ExecutionModel` never imports a calendar, never assumes a fixed
   bars-per-day count, and operates identically on daily, hourly, or
   any other canonical `Interval` -- the Sprint 8 data boundary
   (ADR-0041) is untouched; `ExecutionModel` consumes only the
   already-canonical `candles` frame the caller passes in.

9. **Capital affordability accounts for fees via the smallest
   architecture-compatible addition: a post-hoc check, not a Risk
   redesign.** `PortfolioRiskEngine.decide()`/`decide_close()` are
   completely unmodified -- they still size a trade with no knowledge
   that fees exist. `PortfolioBacktestEngine._unaffordable_reason()`
   compares `portfolio.cash + fill.cash_delta` against zero *after*
   `ExecutionModel.simulate()` produces a real fill, and reports
   `INSUFFICIENT_CASH_FOR_FEE` (a new `SignalOutcome.
   execution_unavailable_reason`, mutually exclusive with
   `stop_unavailable_reason`, requiring an approved `risk_decision`)
   rather than ever letting `Portfolio.cash` go negative. This also
   catches the more general case where `NEXT_BAR_OPEN`'s real fill
   price has moved far enough from the signal bar's own close that the
   risk-approved quantity is no longer affordable at all, fee or no
   fee -- the same rejection path handles both without new machinery.

10. **`BacktestConfig`/`BacktestResult`/`ExperimentSpec` gain execution
    identity with zero registry schema change.** `BacktestConfig.
    execution_config: ExecutionConfig` defaults to a fresh
    `ExecutionConfig()` (zero-cost, `SIGNAL_BAR_CLOSE`) and flows into
    `BacktestConfig.describe()`'s new `"execution"` key alongside the
    existing `risk_mode`/`risk_limits`/`stop_policy` keys. Two configs
    differing only in `slippage_bps` (or only in timing mode) describe
    differently by construction, since `describe()` is a plain,
    complete dict of the config's own values -- no separate hashing
    scheme was needed. `ExperimentSpec.backtest_config` has been a
    reserved, extensible JSON field since ADR-0044 (itself inherited
    from an even earlier precedent) -- the new `"execution"` key
    round-trips through `ExperimentRegistry` with zero schema
    migration, exactly the same free ride ADR-0044's own `quantity`
    provenance got.

11. **AI strategies get zero special-casing, exactly as ADR-0044
    established -- and it is re-verified, not just assumed to still
    hold.** `ExecutionModel` has no knowledge of which `Strategy`
    produced the `Signal` an `Order` traces back to; `test_architecture.
    py`'s existing `test_portfolio_backtest_engine_has_no_ai_specific_
    branch` scan of `portfolio_engine.py` still passes with the new
    execution-routing code added, and a new end-to-end test
    (`test_ai_strategy_runs_through_the_cost_aware_execution_model` in
    `tests/test_ai_end_to_end.py`) runs a trained `AISignalStrategy`
    through a genuinely cost-aware `ExecutionConfig` (nonzero slippage,
    fees, `NEXT_BAR_OPEN` timing) and confirms its trades carry the
    same fill/fee/net-P&L fields any rule-based strategy's trades do.

12. **Legacy compatibility and a zero-cost baseline are both structural
    guarantees, not conventions.** `Backtester.run()`/`RiskMode.
    LEGACY_UNIT` are completely untouched -- `ExecutionModel` is only
    ever constructed inside `PortfolioBacktestEngine`. Every existing
    `PORTFOLIO_RISK` test that never mentions `execution_config`
    continues to pass unchanged, because the default `ExecutionConfig`
    reproduces exactly what `run_portfolio()` always did. Running the
    same signals through the default config and an explicitly-
    constructed zero-cost `ExecutionConfig()` produces numerically
    identical trades
    (`test_default_execution_config_is_backward_compatible_zero_cost_baseline`),
    so a researcher always has a controlled frictionless baseline to
    compare a cost-aware run against (Sprint 12 spec, section 34).

**Non-goals, explicit (Sprint 12 spec, sections 24-27, 59):**

- Partial fills, order-pending/rejected lifecycle states, or any
  quantity shortfall. An approved order is either fully filled or not
  filled at all -- `filled_quantity` is always exactly the risk-
  approved quantity when `filled=True`.
- Limit, stop, stop-limit, IOC, FOK, or GTC orders. The execution model
  supports the existing market-order path only.
- Order-book simulation of any kind: bid/ask ladders, depth, market-
  impact curves, queue position, latency, exchange matching. The model
  stays candle-level; slippage is the sole friction proxy this sprint,
  never double-counted against a separate spread model.
- Broker-specific fee schedules. `ExecutionModel`/`PercentageFeeModel`/
  `PercentageSlippageModel` are generic and broker-independent by
  construction -- `src/backtesting/execution_model.py` imports nothing
  from `src.broker`, Alpaca, IBKR, IG, or Tiger
  (`test_execution_model_does_not_import_broker_adapters_or_src_broker`).
- Any change to `AlpacaBroker`/`IBKRBroker`/`IGBroker`/`TigerBroker` or
  to `PaperBroker`'s own live/paper execution semantics. `PaperBroker`
  was not modified in any way this sprint; its existing tests pass
  unchanged.
- Advanced portfolio optimization or ML risk models of any kind. Not
  touched, not extended.

**Research interpretation (Sprint 12 spec, section 58):** a backtest's
return still does not equal a realized live return, even after this
sprint. This sprint improves how execution friction (timing, slippage,
fees) is represented -- it does not model market impact, latency,
bid/ask microstructure, partial fills, or any specific broker's actual
execution behavior. Those remain explicitly deferred, same as before.

**Sandbox verification:**

Same network-free sandbox as every prior sprint (`sklearn`, `joblib`,
`streamlit` uninstalled; `pip install` fails with a proxy `403`). Using
the sandbox's stub test runner (`/tmp/runner_all.py`), the full suite
reported:

**855 passed, 2 failed, 8 skipped.**

The 2 failures were the same environment-only failures every sprint
since Sprint 7 has carried forward unchanged --
`test_cli_doctor.test_python_version_passes_against_running_interpreter`
and `test_config.test_logger_is_importable_and_callable` -- confirmed
untouched by this sprint's changes. The 8 skips were the expected
`sklearn`/`joblib`/`streamlit` import guards, including the new Sprint
12 AI-compatibility assertion inside `tests/test_ai_end_to_end.py`
(`test_ai_strategy_runs_through_the_cost_aware_execution_model` joined
the pre-existing sklearn-gated tests in that file) -- verified only via
`py_compile`, not execution, in that sandbox.

**Real `pytest` on the dev machine confirms it: 916 passed, 0 failed**
(3.92s, no skips). Neither sandbox-only failure reproduced, and every
scikit-learn/joblib/Streamlit-gated test the sandbox could only skip --
including the new AI-compatibility test this sprint added -- ran for
real and passed. The jump from the sandbox's 855 to 916 real passes
(rather than 855 + 8 = 863) reflects that the sandbox's stub runner
counts an import-gated *module* as a single skip, while real `pytest`
collects and runs every individual test inside those modules once
`sklearn`/`joblib`/`streamlit` are actually installed. Sprint 12 is
fully confirmed.

## ADR-0046: Sprint 13 -- AI Research Agent

**Status:** Accepted and fully closed out. Sandbox-confirmed
(network-free, mock-provider tests only), confirmed via real `pytest`
on the dev machine (**1017 passed, 0 failed**, after one round-trip
fixing a test bug -- see "Real-machine verification" below, both
commits pushed to `origin/main`), and confirmed via a manual smoke test
against the live Anthropic API demonstrating genuine, budget-respecting
tool-selection behavior (see "Real-provider smoke test" below).

**Context:**

Every prior sprint added a new *deterministic* capability -- data,
indicators, strategies, risk, execution, analytics, classic ML. Sprint
13 introduces the platform's first genuinely agentic component: an LLM
that decides, turn by turn, which of a small set of research tools to
call next, rather than following a fixed, hand-coded sequence. This is
qualitatively different from the existing `src.research.ResearchReporter`
(`findings -> narrative`, ADR-0020), which never chooses an action or
iterates -- it renders one already-computed result into prose. The risk
this sprint has to manage is not "can an LLM produce useful research
prose" (already solved) but "can an LLM be given real decision-making
latitude over a trading platform's research surface without that
latitude ever reaching trading, portfolio, risk, or model/strategy
mutation."

**Decision:**

1. **The agent is an orchestration layer, never a domain owner.**
   `src/ai/agents/` (`agent.py`, `models.py`, `policy.py`, `provider.py`,
   `tools.py`, `research_tools.py`, `backtest_tool.py`, `session.py`,
   `store.py`, `prompts.py`, `anthropic_provider.py`) contains no
   backtesting, risk, execution, or analytics logic of its own -- every
   tool delegates to an existing application service
   (`src.experiments.registry.ExperimentRegistry`,
   `src.analytics.service.AnalyticsService`, `src.strategies.registry`,
   `src.ai.registry.ModelRegistry`, and the new
   `src.research.trial_service.ResearchTrialService`). `src.research.
   ResearchReporter` is untouched and has no dependency on this package
   in either direction beyond "the agent may consume its output" (an
   architecture test enforces `src/research` never imports
   `src.ai.agents`).

2. **A new, generic `ResearchTrialService`
   (`src/research/trial_service.py`) -- not agent-specific code.** The
   platform had no clean, reusable, programmatic path composing
   `MarketDataService -> Strategy -> PortfolioBacktestEngine (Risk,
   Execution) -> Analytics` outside of `scripts/run_experiment.py` (an
   application-level convenience script, never meant to be a library
   API, and explicitly not to be shelled out to -- Sprint 13 spec
   section 24). `ResearchTrialService.run_trial()` is that missing
   service: flat, primitive-typed arguments in, a `ResearchTrialOutcome`
   (existing `BacktestResult`/`ExperimentSpec`/`BacktestAnalytics` types,
   composed, never duplicated) out. It implements no backtest, risk, or
   execution logic itself -- it constructs `RiskLimits`/
   `PortfolioRiskLimits`/`ATRStopPolicy`/`ExecutionConfig` from
   primitives and calls `Backtester.run_portfolio()`, exactly as a human
   operator would. Every trial is ephemeral (an in-memory
   `ResearchTrialOutcome`, never auto-persisted to `ExperimentRegistry`)
   -- Sprint 13 spec section 40.

3. **A provider-neutral `LLMProvider` protocol
   (`src/ai/agents/provider.py`), Anthropic as the first concrete
   adapter (`src/ai/agents/anthropic_provider.py`).** `ResearchAgent`
   depends only on `LLMProvider`'s `generate(system, messages, tools) ->
   LLMResponse` contract -- it has no reference to Anthropic, OpenAI, or
   Google anywhere. `anthropic_provider.py` imports the `anthropic`
   package lazily, inside `generate()`, mirroring the exact convention
   `src.research.renderers.ClaudeNarrativeRenderer` already established
   (ADR-0020): `anthropic` is **not** added to `requirements.txt`, so
   the base platform -- including every other test in this suite --
   continues to work with zero setup whether or not the package or an
   API key is present. Configuration (`AI_AGENT_MODEL`,
   `ANTHROPIC_API_KEY`) is read from the environment at construction
   time, never hard-coded. `FakeLLMProvider` (scripted responses, no
   network) is what makes the entire agent loop -- including every
   scenario in `tests/test_ai_agent_loop.py` -- testable without a
   network connection or an API key.

4. **`ResearchAgentPolicy` enforces permissions and budgets in code,
   never in the prompt alone.** `max_steps` (default 12) and
   `max_backtests` (default 4) are enforced by `ResearchAgent._loop()`/
   `_handle_tool_call()` itself, not requested of the model. The four
   dangerous flags (`allow_live_trading`, `allow_portfolio_mutation`,
   `allow_strategy_generation`, `allow_model_training`) must be `False`
   -- `ResearchAgentPolicy.__post_init__` raises if any is `True`,
   because there is no tool in this sprint's tool surface that flag
   could safely gate at all (Sprint 13 spec section 92: "the answer must
   be structurally impossible because those tools are not present," not
   merely policy-denied). `ToolRegistry.allowed_definitions()` means a
   forbidden tool is never even *described* to the model, let alone
   executable.

5. **A minimal, explicit tool surface -- seven tools, no more.**
   `list_experiments`, `get_experiment`, `analyze_experiment`,
   `compare_experiments`, `list_strategies`, `get_model_metadata`
   (`src/ai/agents/research_tools.py`) are read-only and delegate
   entirely to existing services -- none recomputes a metric or a
   comparison `AnalyticsService`/`ExperimentRegistry` already computes.
   `run_historical_backtest` (`src/ai/agents/backtest_tool.py`) is the
   one tool that produces new evidence; it validates every input
   (registered strategy, valid interval, parseable historical-only
   dates within a 10-year bound, at most 10 plain-primitive strategy
   params, a plausible ticker symbol) before ever calling
   `ResearchTrialService`, and its JSON-schema's `properties` admit no
   filesystem path, URL, shell command, broker name, or credential field
   at all -- `ToolRegistry.execute()`'s schema validation rejects any
   unlisted field outright (Sprint 13 spec section 27). There is no
   `submit_order`/`cancel_order`/`modify_portfolio`/`modify_risk`/
   `train_model`/`modify_model`/`write_strategy`/`write_file`/
   `execute_shell`/`arbitrary_http`/`browse_web` tool anywhere in this
   package -- `tests/test_ai_agent_loop.py`'s permission-boundary tests
   assert this structurally (`ToolRegistry.execute()` on any such name
   returns `UNKNOWN_TOOL`), not merely that policy would deny it.

6. **An explicit, bounded state-machine loop
   (`src.ai.agents.agent.ResearchAgent._loop()`), not a hard-coded
   sequence with an LLM narrating it.** Every iteration: send the
   conversation to the provider; if it returned tool calls, validate and
   execute each through `ToolRegistry.execute()` (policy check + schema
   validation baked in), append the result, and continue; otherwise, the
   model's text is the final answer. `tests/test_ai_agent_loop.py`
   proves at least two genuinely different valid paths (a
   zero-tool-call early stop, a single-lookup path, a two-backtest
   comparison path, and a budget-exhaustion path) rather than one fixed
   script with an LLM in front of it (Sprint 13 spec section 83).

7. **`AgentRun`/`AgentResearchReport`/`TrialResult`/`AgentEvidence`
   (`src/ai/agents/models.py`) are the audit and provenance record.**
   Every timestamp is timezone-aware UTC. `AgentRun.run_id` is a fresh
   UUID (an event, not a reusable spec) but `system_prompt_version`,
   `tool_schema_version` (a hash of the exact tool definitions offered),
   and `policy_hash` (a hash of the enforced policy) together give full
   configuration provenance -- two runs against identical platform data
   can still be told apart if the agent's own configuration differed
   (Sprint 13 spec sections 11-12). No hidden chain-of-thought is ever
   persisted -- the dataclasses simply have no field for it; only goal,
   tool calls/arguments/result summaries, trial/experiment ids, the
   final report, and errors are recorded. `AgentRunStore`
   (`src/ai/agents/store.py`) persists one JSON file per run under
   `data/agent_runs/` -- already covered by `.gitignore`'s existing
   `data/*` rule, so no runtime agent history is ever committed.

8. **Three terminal statuses, never collapsed into one.**
   `AgentRunStatus.COMPLETED` (a final answer was produced within
   budget), `FAILED` (a `ProviderError` occurred -- missing API key,
   missing `anthropic` package, network failure, malformed response,
   timeout -- before or during research), and `BUDGET_EXHAUSTED`
   (`max_steps` or `max_backtests` was reached first). A
   `BUDGET_EXHAUSTED` run still returns whatever partial
   `AgentResearchReport` was gathered, with `limitations` explicitly
   stating the investigation is incomplete -- it is never presented as a
   completed one.

9. **Observation vs. hypothesis is a labeling convention the system
   prompt establishes and `ResearchAgent._build_report()` extracts
   mechanically** (`Observed:`/`Hypothesis:`/`Suggested next
   experiment(s):` line prefixes) -- deliberately simple, not an attempt
   at general natural-language understanding.
   `AgentResearchReport` has no `trade_recommendation`/
   `buy_sell_recommendation` field at all (Sprint 13 spec sections
   45-46) -- `suggested_next_experiments` may only ever propose further
   research.

10. **A `python -m src.cli research-agent --goal "..."` entry point**
    (`src/cli/research_agent.py`, wired into `src/cli/__main__.py` via a
    new `ARGV_COMMANDS` mapping alongside the existing zero-argument
    `COMMANDS`, so `doctor`'s contract is untouched). Fails with an
    actionable, explicit message (exit code 2) when `ANTHROPIC_API_KEY`
    is absent -- it never silently falls back to the deterministic
    `ResearchReporter` and calls that "the agent ran."

11. **No web/filesystem/shell tool, no multi-agent system, no long-term
    memory, no automated model/strategy mutation -- all explicitly
    deferred, not half-implemented.** `AgentSession` (`session.py`)
    holds only the current run's goal, messages, tool results, trials,
    and evidence -- no vector store, no embeddings, no cross-run memory.
    One agent, one provider call at a time (tool calls within one
    provider response are processed sequentially, never concurrently).

12. **Historical-only, enforced twice.** `ResearchTrialService.run_trial()`
    rejects an `end` date in the future; `RunHistoricalBacktestTool`
    checks the same constraint again before ever calling the service
    (defense in depth -- Sprint 13 spec section 28). Neither this tool
    nor anything else in `src/ai/agents` imports `src.broker`,
    `src.execution.engine` (`PaperBroker`), or a `DataProvider`
    implementation directly (`yfinance` included) -- market data is only
    ever reached through `MarketDataService`, an architecture test
    enforces this (`tests/test_architecture.py`,
    `test_agent_core_does_not_import_yfinance_or_any_data_provider_directly`).

**Non-goals (explicitly deferred, per the Sprint 13 spec):** a
Strategy Development Agent (code-generating strategies), a Market
Monitoring Agent (a background/scheduled agent), LLM-based
trading-context signal generation (`AISignalStrategy` remains Sprint
10's classic-ML implementation, untouched), a controlled trading agent,
multi-agent orchestration, vector-database/RAG memory, automated model
retraining, and an agent-specific dashboard page. Each is real future
work with an established seam (the provider abstraction already
supports adding OpenAI/Google/a local model without touching the tool
layer or policy), not something this sprint half-builds.

**Sandbox verification:**

Same network-free sandbox every sprint since Sprint 9 has used
(`sklearn`, `joblib`, `streamlit` uninstalled; no network for `pip
install`). Every new Sprint 13 test file (`tests/test_ai_agent_core.py`,
`tests/test_ai_agent_research_tools.py`,
`tests/test_research_trial_service.py`,
`tests/test_ai_agent_backtest_tool.py`, `tests/test_ai_agent_loop.py`,
plus new `tests/test_architecture.py` additions) uses only
`FakeLLMProvider` -- zero network access anywhere in the new suite,
matching Sprint 13 spec section 73. Four of the five new test files
(everything touching `src.ai.registry.ModelRegistry`, which imports
`joblib` at module scope) are `pytest.importorskip("joblib")`-gated in
this sandbox, the same established convention every sklearn/joblib/
streamlit-gated module in this codebase already uses. With those
guards in place, the full suite reported:

**897 passed, 2 failed, 13 skipped.**

The 2 failures are the same environment-only failures every sprint
since Sprint 7 has carried forward unchanged --
`test_cli_doctor.test_python_version_passes_against_running_interpreter`
and `test_config.test_logger_is_importable_and_callable`. Of the 13
skips, 5 are the pre-existing sklearn-gated AI test modules, 1 is the
pre-existing streamlit-gated dashboard smoke test, and 7 are this
sprint's own joblib-gated additions (`test_ai_agent_backtest_tool.py`,
`test_ai_agent_loop.py`, `test_ai_agent_research_tools.py`,
`test_research_trial_service.py`, plus 3 pre-existing
`tests/test_architecture.py` skips including the new
`test_default_tool_registry_exposes_exactly_the_seven_sprint13_tools`).
Every one of the Sprint 13 skips is expected to run and pass for real
once `joblib`/`scikit-learn` are installed (the dev machine's `.venv`
already has them, per Sprint 10-12's own real-pytest confirmations).

**Real-machine verification:**

The operator's first real `pytest` run reported **1016 passed, 1
failed** -- every sklearn/joblib-gated test the sandbox could only skip
ran for real, including the rest of this sprint's own new agent test
files, all passing. The one failure was a bug in the test itself, not
the implementation: `tests/test_research_trial_service.py::
test_run_trial_produces_full_provenance` asserted `outcome.spec.
dataset_fingerprint == "fake-content-hash"`, a literal the test's own
fixture had set on `CandleDataset.content_hash` -- but `ExperimentSpec.
capture()` never reads that field; it always computes
`dataset_fingerprint` itself by hashing the actual candle values
(`src.utils.hashing.dataframe_fingerprint`, see decision 2 above and
`src/experiments/spec.py`). This test class only runs where `joblib` is
installed, so the sandbox's own 897-passed run above never executed it
-- exactly the situation the sandbox-vs-real-machine split exists to
catch. Fixed by asserting against `dataframe_fingerprint(candles)`
computed from the same fixture data. The operator's second real-pytest
run, with the fix applied, confirmed **1017 passed, 0 failed** (4.82s).
Both commits (`eea2ccf`, the implementation, and `83e60b9`, the test
fix) are pushed to `origin/main`. Do not treat the sandbox count above
as final release verification (established convention, every sprint
since Sprint 8).

**Real-provider smoke test:**

Run against the live Anthropic API (`python -m src.cli research-agent
--goal "Investigate whether the EMA strategy's drawdown is concentrated
in volatile regimes"`, run ID `843dffa5747a4eba803f3928a287e5b0`). The
model demonstrated genuine tool-selection behavior rather than a
scripted sequence: it called `list_strategies`, then probed
`list_experiments` with a couple of different filter-argument guesses
before finding the right call shape, then chose four distinct
historical windows to backtest on its own initiative (the full
2018-2022 range, then 2019, the 2020 COVID crash, and 2021) to compare
drawdown behavior across regimes -- exactly the kind of investigative
path this sprint's spec required proof of (at least two genuinely
different valid paths, Sprint 13 spec section 83), now demonstrated
live rather than only against `FakeLLMProvider`. On its 5th tool call
attempt it was cleanly rejected with `BUDGET_EXHAUSTED`
(`max_backtests=4` enforced by the runtime, not the model), and the run
terminated in that exact status, reporting "any observations above are
based on incomplete research, not a completed investigation" rather
than presenting a fabricated conclusion on partial evidence. This
confirms decision 8 above (`BUDGET_EXHAUSTED` as a distinct, honest
terminal status) end-to-end against the real provider, not just the
fake one. Sprint 13 is fully closed out.


## ADR-0047: Sprint 14 -- AI Strategy Development Agent

**Status:** Accepted and fully closed out. Sandbox-confirmed (**1008
passed, 2 failed (pre-existing, environment-only), 17 skipped**),
confirmed via real `pytest` on the dev machine (**1179 passed, 0
failed**, 91.41s, both commits pushed to `origin/main`), and confirmed
via two real-provider smoke tests against the live Anthropic API -- the
first surfaced a genuine prompt-design bug (fixed and re-confirmed
below), the second completed the full candidate lifecycle end-to-end
through to a human-approved promotion (see "Real-provider smoke test,
round 1" and "round 2" below).

**Context:**

Sprint 13's `ResearchAgent` (ADR-0046) can investigate but cannot
create: its tool surface is read-only plus one historical backtest
tool, and its policy structurally forbids strategy generation. Sprint
14 asks for the next capability up the same trust ladder --  an agent
that can write, test, and iterate on new candidate trading strategies
of its own design -- while drawing a boundary that must hold even
against a fully compromised or adversarial LLM, not just a
well-behaved one: an AI may **CREATE**, **TEST**, **ITERATE**, and
**REPORT** on a candidate strategy; an AI may never **PROMOTE** it into
anything the platform would actually trade, **MODIFY** the production
`StrategyRegistry`, or execute a live order. Sprint 13's policy-object
pattern (`allow_live_trading` hard-denied) proved the concept for
"deny an action the model never had a tool for"; Sprint 14 has to prove
the harder case -- an agent whose entire *job* is to generate and run
arbitrary Python, where "arbitrary Python" is exactly the thing that
could otherwise defeat any code-level safety check by construction.

**Decision:**

1. **A parallel, quarantined strategy namespace -- `CandidateStrategy`,
   never `Strategy` (`src/ai/agents/strategy_dev/models.py`).**
   Candidate code is never registered in `src.strategies.registry`
   (the production `StrategyRegistry` used by `Backtester`/`PaperBroker`
   for anything the platform actually runs unattended) and never
   becomes an instance the agent can hand directly to
   `PortfolioBacktestEngine`. Its evidence -- source hash, spec hash,
   lifecycle status, every stage's `CandidateEvaluation` -- lives in its
   own `CandidateStrategy` dataclass and its own `CandidateRegistry`
   (`workspace.py`), a completely separate store from
   `ExperimentRegistry`. Nothing in `src/strategies/registry.py` gained
   a single new line for this sprint -- an architecture test
   (`test_strategy_registry_has_no_dependency_on_the_agent_package`)
   proves the production registry has zero awareness this package
   exists.

2. **An explicit, one-directional lifecycle state machine
   (`CandidateStrategyStatus`), with immutability enforced the moment a
   candidate is frozen.** `DRAFT -> VALIDATED -> DEV_BACKTESTED ->
   REVISED (loops back to VALIDATED) -> FROZEN -> OUT_OF_SAMPLE_TESTED
   -> REVIEW_REQUIRED -> PROMOTED | REJECTED | ABANDONED` (exact edges
   enumerated once, in `workspace.py`, and proven exhaustively in
   `tests/test_strategy_dev_workspace.py`: every legal transition
   succeeds, every illegal target from every state is rejected). Once a
   candidate reaches `FROZEN`, its source file and every field on its
   spec become immutable (`CandidateWorkspace.write_source()` refuses to
   overwrite an existing candidate's source at all, ever, regardless of
   status) -- freezing exists specifically to give the final,
   out-of-sample test something the agent cannot have quietly reshaped
   after seeing how well it did.

3. **The test-set lock is enforced twice, independently, at two
   different layers -- defense in depth, not a single choke point.**
   `RunCandidateBacktestTool.execute()` (the tool layer) refuses a
   `stage="final_test"` call unless the candidate's status is exactly
   `FROZEN`, and separately refuses if a final-test evaluation already
   exists. `CandidateRegistry.record_evaluation(stage="final_test", ...)`
   (the storage layer) raises `CandidateImmutableError` under the same
   conditions, entirely on its own, with no dependency on the tool
   having checked anything first. `tests/test_strategy_dev_workspace.py`
   proves the storage layer holds even if a hypothetical future tool
   bug (or a bypass of `RunCandidateBacktestTool` entirely) tried to
   call the registry directly. A held-out test set an agent can query
   more than once by construction is not a held-out test set; this
   sprint treats "ran the final test twice, saw the second result, kept
   iterating" as exactly as serious a violation as "wrote to the
   production registry," and closes both the same way -- in code, at
   more than one layer.

4. **A static AST safety validator (`safety.py`) runs before any
   candidate code ever executes, and only an explicit allowlist of
   platform modules may be imported.** `validate_candidate_source()`
   parses (never executes) candidate source and rejects: any import
   outside `ALLOWED_PLATFORM_MODULES` (an explicit, exact-match set --
   `src.strategies.sdk`/`src.strategies.base`/`src.signals.models`/
   `src.indicators.engine` and a short list of stdlib data types, never
   a prefix match, so `import src.strategies.registry` fails even
   though the SDK modules are allowed); any relative import; any
   dynamic-execution construct (`eval`, `exec`, `compile`, `__import__`,
   `getattr`/`setattr`/`delattr`, `globals`/`locals`/`vars`, `open`,
   `input`); any sandbox-escape gadget attribute access
   (`__subclasses__`, `__bases__`, `__mro__`, `__globals__`,
   `__builtins__`, `__code__`, `__closure__`, `__reduce__`,
   `__reduce_ex__`, `__dict__`); any attempt to reference or call
   `register_strategy`/the production registry by name, imported or
   not; oversized source; and malformed class shape (not exactly one
   class, missing the required base, multiple inheritance, missing a
   required method). Violations are collected and reported together,
   not just the first one found, so a candidate with four simultaneous
   problems gets told about all four. `tests/test_strategy_dev_safety.py`
   (47 tests) exercises every one of these categories with a
   representative bypass attempt, not just the obvious case.

5. **A controlled subprocess runner (`runner.py` + `_harness.py`) is the
   only place already-validated candidate code actually executes, and
   it is explicit about being defense-in-depth, not a hardened
   sandbox.** `run_candidate()` launches `_harness.py` in a fresh
   subprocess with a sanitized environment (`PATH` + `PYTHONPATH` only
   -- the parent's environment is replaced, never inherited-and-filtered,
   so `ANTHROPIC_API_KEY` and everything else the parent process holds
   is structurally absent from the child), a hard `timeout` (default
   30s, `subprocess.TimeoutExpired` is caught and reported as
   `RunnerResult(timed_out=True)`, never left to hang the caller), and
   candles crossing the process boundary as plain JSON records, never
   pickle. `_harness.py` catches every exception from candidate code and
   reports `f"{type(exc).__name__}: {exc}"`, never propagating a raw
   traceback or crashing. This is combined with, not a substitute for,
   decision 4's static checks -- the module's own docstring is explicit
   that the subprocess still shares the host's filesystem and network
   stack, so real containment comes from candidate code never
   containing a network/filesystem/subprocess-capable import in the
   first place. `tests/test_strategy_dev_runner.py` proves both halves
   independently: the harness's own parsing/error-reporting contract
   in-process, and, through a real subprocess, that the sanitized
   environment genuinely never exposes a parent secret, that a
   pathological (sleeping) candidate is actually killed near the
   requested timeout rather than hanging, and that a candidate
   attempting a network call to a non-routable address fails closed and
   is reported as a structured error rather than crashing the harness.

6. **`PrecomputedSignalStrategy` keeps the Backtester ignorant of
   candidates entirely (Sprint 14 spec: "do not add `if strategy is
   CandidateStrategy` inside core Backtester logic").** A candidate's
   `prepare()`/`generate_signals()` run once, safely, inside the
   isolated subprocess; by the time a backtest actually happens,
   `PrecomputedSignalStrategy` wraps the already-produced, plain
   `list[Signal]` as an ordinary `Strategy` (satisfying the same
   `Protocol` any hand-written strategy does) and hands it to
   `PortfolioBacktestEngine.run_portfolio()` completely unmodified.
   `tests/test_architecture.py::test_backtester_has_no_candidate_specific_branch`
   greps `engine.py`/`portfolio_engine.py` for any of `CandidateStrategy`,
   `PrecomputedSignalStrategy`, `strategy_dev`, or `src.ai.agents` and
   asserts none appear -- the core backtesting engine that every prior
   sprint built and tested is provably untouched by this one.

7. **`StrategyDevelopmentAgentPolicy` hard-denies every dangerous
   capability, the same pattern Sprint 13 established.** No tool in
   this sprint's fourteen-tool surface can submit a live order, mutate
   a production portfolio, write to `StrategyRegistry`, or call
   `CandidateRegistry.promote()` -- promotion is not merely
   policy-denied, it is a method the agent's own tool surface has no
   path to reach at all (`tests/test_strategy_dev_tools.py::
   test_no_tool_wraps_candidate_registry_promote` checks the literal
   absence of `.promote(` in `tools.py`). Budgets (`max_candidates`,
   `max_candidate_revisions`, `max_validation_backtests`,
   `max_final_test_evaluations`, `max_steps`) are enforced by
   `StrategyDevelopmentAgent._loop()` itself, exactly as Sprint 13's
   `max_steps`/`max_backtests` were -- never left to the model's own
   restraint.

8. **Promotion is a separate, human-only CLI
   (`src/cli/strategy_promote.py`), structurally unreachable from agent
   code.** `strategy-promote` prints full evidence (status, hashes,
   every stage's evaluation, agent run provenance, optional full
   source) and requires an interactive `y/N` confirmation (or explicit
   `--yes`) before calling `CandidateRegistry.promote()` -- and refuses
   outright unless the candidate's status is `REVIEW_REQUIRED` with a
   recorded final-test evaluation. Promoting only marks the candidate
   `PROMOTED` in the candidate registry; it does not touch
   `StrategyRegistry`, deploy anything, or place a trade -- turning a
   promoted candidate into something the platform actually runs remains
   a deliberate, separate, human action outside this sprint's scope,
   exactly as this sprint's safety principle requires.
   `tests/test_architecture.py::
   test_strategy_promote_is_never_imported_by_the_agent_package` proves
   `strategy_promote.py` has no incoming edge from `src.ai.agents.
   strategy_dev` anywhere in the source tree.

9. **Lazy attribute resolution (PEP 562) keeps the human-only
   promotion path free of the agent's heavy ML dependency chain.**
   `src/ai/agents/strategy_dev/__init__.py`'s eager imports were
   replaced with a `__getattr__`/`__dir__` pair resolving each public
   name to its owning module only on first access; `src/cli/__main__.py`
   similarly defers `strategy_dev.py`/`strategy_promote.py`'s own
   imports into per-command closures. Before this fix, importing
   anything from `strategy_dev` -- including from the promotion CLI,
   which never touches `ResearchTrialService` or `ModelRegistry` --
   forced the full `ModelRegistry -> joblib` chain to load regardless.
   After: `strategy-promote` runs and works with neither `joblib` nor
   `scikit-learn` installed, which this sandbox's own testing (`atp
   strategy-promote` exercised end-to-end here, `joblib`-free) directly
   demonstrates -- the human approval path has no reason to depend on
   whichever ML library the candidate's own tooling happens to use.

**Non-goals (explicitly deferred, per the Sprint 14 spec):** automated
promotion or deployment of any kind, live or paper trading by a
candidate, portfolio or risk-config mutation by the agent, a second
"critic" agent, genetic/evolutionary strategy search, any relaxation of
the static safety allowlist to admit filesystem, network, or subprocess
imports, and a dashboard page for candidates. Each remains a real,
separately-scoped future decision, not something this sprint
half-implements under a different name.

**Sandbox verification:**

Same network-free, `joblib`/`scikit-learn`/`streamlit`-uninstalled
sandbox every sprint since Sprint 9 has used. This sprint's five new
test files -- `tests/test_strategy_dev_tools.py` (37 tests),
`tests/test_strategy_dev_agent_loop.py` (12 tests),
`tests/test_strategy_dev_workspace.py` (29 tests),
`tests/test_strategy_dev_safety.py` (47 tests), and
`tests/test_strategy_dev_runner.py` (19 tests) -- plus 21 new
`tests/test_architecture.py` additions, total **165 new tests**. Only
the two files that transitively import `ModelRegistry` (`tools.py` ->
`ResearchTrialService` -> `src.ai.registry`) are joblib-gated
(`test_strategy_dev_tools.py`, `test_strategy_dev_agent_loop.py`) --
`workspace.py`, `models.py`, `safety.py`, and `runner.py`/`_harness.py`
have zero heavy dependencies and run for real in this sandbox with no
guard needed. With those two gated, the full suite reported:

**1008 passed, 2 failed, 17 skipped.**

The 2 failures are the same environment-only failures every sprint
since Sprint 7 has carried forward unchanged --
`test_cli_doctor.test_python_version_passes_against_running_interpreter`
and `test_config.test_logger_is_importable_and_callable`, both entirely
unrelated to this sprint's changes (neither `test_cli_doctor.py` nor
`test_config.py` was touched). Of the 17 skips, 15 are the pre-existing
sklearn/joblib/streamlit-gated modules carried forward from prior
sprints, and 2 are this sprint's own joblib-gated additions
(`test_strategy_dev_tools.py`, `test_strategy_dev_agent_loop.py`),
expected to run and pass for real once `joblib`/`scikit-learn` are
installed (the dev machine's `.venv` already has them, per Sprint
10-13's own real-pytest confirmations).

**Real-machine verification:**

Real `pytest` on the dev machine confirmed **1179 passed, 0 failed**
(91.41s) -- every joblib/scikit-learn-gated test the sandbox could only
skip ran for real, including both of this sprint's own gated files
(`test_strategy_dev_tools.py`, `test_strategy_dev_agent_loop.py`), all
passing on the first attempt. The run surfaced one cosmetic
`PytestCollectionWarning` (not a failure): `tools.py`'s
`TestCandidateTool` -- an agent tool class named after the
`test_candidate` lifecycle action, unrelated to pytest -- was picked up
by pytest's default `Test*` collection heuristic because it has an
`__init__`. Fixed by adding `__test__ = False` to the class, which
pytest respects to opt a class out of collection without touching its
name or behavior; re-confirmed against `tests/test_strategy_dev_tools.py`
(37/37 still passing). Pushed to `origin/main`
(`873f20d..ee0b605`).

**Real-provider smoke test, round 1 (bug found):**

Run against the live Anthropic API (`python -m src.cli strategy-dev
--goal "Create a simple momentum strategy on QQQ using a short EMA
cross with a volatility filter, backtest it, and freeze it if it looks
reasonable"`, run ID `5f70eb5d794e4b57a3e28e9299ed1d13`). The model
correctly inspected existing strategies/indicators/experiments first
(`list_strategies`, `list_indicators`, `list_experiments` x2), designed
a thoughtful, well-specified candidate (an ATR-volatility-gated EMA
cross with a clearly stated research question and hypothesis), then
called `create_candidate_strategy` -- and never succeeded. It hit
`INVALID_ARGUMENTS: missing required field 'source'` on its first
attempt (also missing `entry_logic`), added `entry_logic` on the second
attempt, but then repeated the *exact same* `INVALID_ARGUMENTS: missing
required field 'source'` failure on 9 further attempts in a row,
elaborating spec-field wording each time but never once including the
`source` field, until `BUDGET_EXHAUSTED` at 16 steps -- zero candidates
created.

Root cause: `SYSTEM_PROMPT` (`prompts.py`, v1) told the model to write
candidate source but gave it no concrete example of what that source
should look like -- no import list, no class-shape template, nothing
showing the exact `BaseStrategy` pattern `safety.py`'s static validator
requires. Confronted with a real (not scripted) research question, the
model spent its effort refining prose fields and never converged on
writing an actual Python class, and-critically-never adapted across
retries despite an unambiguous, identical tool error every time. This
is a real product gap Sprint 13's own smoke test methodology exists to
catch: `FakeLLMProvider`'s scripted responses in
`tests/test_strategy_dev_agent_loop.py` always supply a well-formed
`source` string, so no unit test could have surfaced this -- only a
live model working from the system prompt alone could.

**Fix:** `prompts.py` (bumped to `SYSTEM_PROMPT_VERSION =
"strategy-dev-agent-system-prompt-v2"`) now states plainly that
`source` is required and must be the complete real Python source (not
a description, not deferred), explicitly lists the five
`ALLOWED_PLATFORM_MODULES` the candidate may import, and includes a
concrete, minimal, working `BaseStrategy` subclass template (an
EMA-cross skeleton mirroring `src/strategies/sdk.py`'s own docstring
example) for the model to pattern-match against. The template was
verified to pass `safety.validate_candidate_source()` cleanly (no
failed checks, one harmless missing-docstring warning) before being
added to the prompt. No test asserts the exact system-prompt version
string, so the bump required no test changes; the full sandbox suite
re-ran clean (1008 passed, 2 pre-existing unrelated failures, 17
skipped) after the change.

**Real-provider smoke test, round 2 (confirmed working):**

Same goal, re-run against the live Anthropic API (run ID
`3e8ee5af9206434e9b1f1b81f7c283c8`). The model again inspected existing
strategies/indicators/experiments first, then called
`create_candidate_strategy` -- this time missing only `signal_semantics`
on its first attempt, and self-correcting on the very next call by
including both `signal_semantics` and a complete, correct `source`
field: a `BaseStrategy` subclass (`EmaCrossVolFilterStrategy`) importing
only `src.strategies.sdk`/`src.signals.models`, computing EMA(8)/EMA(21)
and an ATR(14)-based volatility-percentage filter, and emitting sparse
LONG/FLAT signals via `self.emit_signal(...)` -- confirming the v2
prompt fix worked, and that the model can now recover from a genuine
missing-field error in one retry (unlike the source-field failure mode,
where it never recovered across ten attempts, because it lacked any
template to correct toward). The run then completed the full lifecycle
end-to-end: `validate_candidate` -> `test_candidate` -> a development
backtest (2018-2022, 27 trades, Sharpe 0.47) -> a validation backtest
(2023-2024H1, 7 trades, Sharpe 1.70) -> `freeze_candidate` -> exactly
one final out-of-sample test (2024H2-2025H1, 5 trades, Sharpe 1.11) ->
a full report, terminating `COMPLETED` (not `BUDGET_EXHAUSTED`) with
the candidate at `REVIEW_REQUIRED`. The report itself honestly flagged
its own weaknesses unprompted -- small sample sizes across all three
windows, zero fees/slippage assumed, no baseline comparison against the
existing `ema_cross` strategy, and an untuned fixed volatility
threshold -- and explicitly declined to call the candidate
"promotion-ready," consistent with decision 8's human-only promotion
principle holding under a real model's own incentive to look successful.

The operator then ran `python -m src.cli strategy-promote
--candidate-id 241a3b657ba846169476e8aa374235a1` and confirmed the
full promotion-CLI contract end-to-end: full evidence printed
(status, description, source/spec hashes, agent run id, all three
stage evaluations with trade counts and the final test's exact date
range) before any prompt, an explicit interactive `y/N` confirmation
required (the operator answered `y` as a genuine human decision),
`PROMOTED` recorded with full lineage (`candidate_id`, `source_hash`,
`spec_hash`, `agent_run_id`, `approved_at`), and an explicit closing
statement that promotion only marks the candidate registry -- it does
not register anything in `StrategyRegistry`, deploy anything, or place
a trade. Sprint 14 is fully closed out: implemented, sandbox-verified,
real-pytest-verified (1179 passed, 0 failed), and now real-provider-
verified end-to-end through to a human-approved promotion, with the
one bug the smoke test found (round 1) already fixed and re-confirmed
(round 2).
