# ai-trading-platform

A trading platform organized around **capabilities**, not strategies. AI is
one module among many (`src/ai/`) -- not the center of the project.

```
src/
    data/        - market data acquisition (built first; everything depends on it)
    indicators/  - technical indicators computed from data
    strategies/  - trading logic that consumes data + indicators
    broker/      - broker/exchange connectivity
    execution/   - order routing and execution
    risk/        - position sizing, risk limits, exposure checks
    analytics/   - performance measurement, reporting
    ai/          - ML/LLM-based components
    dashboard/   - Streamlit UI
    utils/       - shared helpers (logging, config, etc.)
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running tests

```bash
pytest
```

## The Market Data Service

The first component built in this project. Its only job:

```python
from src.data import MarketDataService

service = MarketDataService()
spy = service.get_candles("SPY")
```

Everything else -- backtesting, live trading, the dashboard, AI research --
depends on this service rather than on any specific data vendor. Yahoo
Finance is the default provider today (`src/data/yfinance_provider.py`).
Moving to Polygon, Interactive Brokers, or any other vendor means writing a
new class that implements `DataProvider` (`src/data/base.py`) and passing
it into `MarketDataService(provider=...)` -- no other code changes.

Results are cached to `data/cache/` as CSV, keyed by symbol, interval, and
date range, so repeated calls during development don't re-hit the network.
Pass `use_cache=False` to bypass this on a per-call basis.

### API

```python
service.get_candles(
    symbol: str,
    start: date | None = None,   # defaults to 1 year before `end`
    end: date | None = None,     # defaults to today
    interval: Interval | str = Interval.DAY_1,
    use_cache: bool | None = None,
) -> pd.DataFrame
```

Returns a DataFrame indexed by a `timestamp` `DatetimeIndex` with columns
`open, high, low, close, volume` -- sorted ascending, no duplicate rows.
