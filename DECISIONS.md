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
