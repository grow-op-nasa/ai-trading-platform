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
    ai/          - ML/LLM-based components (ai/agents/ = research-only AI agent)
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

## Monitoring

```
python -m src.cli monitor --once                  # deterministic, no API key
python -m src.cli market-monitor-agent --goal "..."   # needs ANTHROPIC_API_KEY
```

`monitor --once` runs seven checks against the persisted paper state and
exits 0 (ok), 1 (warning/critical), 2 (error) or 3 (unknown). A check that
cannot be evaluated is reported as such, never as healthy. Nothing runs in
the background. The agent only reads and explains; it cannot trade, change
state or promote anything. See `DECISIONS.md` ADR-0050.

### Running monitoring on a schedule (Sprint 16)

```bash
python -m src.cli monitor-scheduler --experiment-id 1 --interval-seconds 900     # until Ctrl+C / SIGTERM
python -m src.cli monitor-scheduler --experiment-id 1 --interval-seconds 900 --max-runs 3
python -m src.cli monitor-scheduler --status [--schedule-id exp-1]              # read-only history
```

Deterministic monitoring only: it never calls Anthropic or any broker, so a timer costs no LLM
credits. The first run is immediate (`--no-immediate` waits one interval). Runs never overlap;
ticks missed during a long run or a suspend are skipped and recorded, not replayed. One scheduler
per schedule id (a lock beside the monitoring DB refuses a second). The registry and monitoring DB
must already exist; the scheduler never invents a portfolio. It does not auto-start and does not
survive reboots on its own: run it under systemd/launchd/tmux. Exit codes: 0 clean, 1 attempt
errors, 2 config/storage, 3 lock held. There is no alert delivery yet.

### Alerts (Sprint 17)

Off by default. Enable with environment variables (never commit them):

```bash
export ATP_ALERTS_ENABLED=true ATP_ALERT_SMTP_HOST=smtp.example.com ATP_ALERT_FROM=atp@example.com \
       ATP_ALERT_TO=you@example.com ATP_ALERT_SMTP_USERNAME=... # password: read -rs ATP_ALERT_SMTP_PASSWORD
python -m src.cli alerts config                       # validate (never prints the secret)
python -m src.cli alerts preview --sample NEW         # render, send nothing
python -m src.cli monitor --once --experiment-id 1    # queues events, does not send
python -m src.cli alerts dispatch --once              # deliver; retries use backoff
python -m src.cli alerts status | retry --all-failed
python -m src.cli alerts test-send --to me@example.com [--confirm-send]   # needs ATP_ALERT_TEST_RECIPIENTS
python -m src.cli monitor-scheduler --experiment-id 1 --interval-seconds 900 --dispatch-alerts
```

Alerts fire for new findings, strictly higher severity, and valid resolutions (an unevaluable check never "resolves" anything).
Delivery is at-least-once: a crash right after the server accepts a message can cause one duplicate. `DELIVERED` means the SMTP
server accepted it. `alerts` exit codes: 0 ok, 1 pending/retry, 2 config/usage, 3 failed deliveries. Validation:
`python scripts/scheduler_validation.py` (isolated synthetic fixture, not your real portfolio) and
`python scripts/alert_delivery_validation.py` (local sink; proves the platform, not a real mail provider).

Failure-state check (synthetic injected fault, not a real outage; `--skip-agent` needs no key):

```bash
python scripts/monitoring_failure_live_integration.py --skip-agent
python scripts/monitoring_failure_live_integration.py     # genuine Anthropic run; needs ANTHROPIC_API_KEY
```
