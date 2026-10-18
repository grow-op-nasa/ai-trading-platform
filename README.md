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

For the local application use the guided path below (`scripts/setup_app.sh` does the same and more).

## Local application (Sprint 19) -- first-use guide

A single-user, local, **paper-mode** browser app over the platform's existing research, paper-trading, monitoring and alert services.
It cannot place real orders, and it never calls an AI provider.

**Requirements.** macOS or Linux, Python 3.12 or newer (`requires-python` in `pyproject.toml`), and the packages pinned in `requirements.txt`.
Internet access is needed only when you run a preflight or valuation (market data via yfinance), or if you enable email alerts.

**1. Get or update the code.** `git pull origin main` in the project folder. Updating never touches `data/`, `logs/` or `.env` (all git-ignored).

**2. One-time setup** (safe to repeat):

```bash
./scripts/setup_app.sh
```

It creates `.venv`, installs the pinned requirements, makes empty `data/` and `logs/`, and copies `.env.example` to `.env` only if `.env` does not exist.
It never resets or creates a database or portfolio, runs a backtest, sends email, or calls an AI.

**3. Launch** (and stop with Ctrl+C):

```bash
./scripts/start_app.sh
```

Open `http://localhost:8501` (a browser window opens unless you pass `--no-browser`). The server listens on `127.0.0.1` only. Other options:
`--port N`, `--check` (validate and exit), `--workdir DIR` (use another existing data root). A non-loopback `--address` is refused unless you also pass
`--allow-remote`; the app has no authentication, so do not do that on a shared network.

**4. Configuration.** Market data needs no key (yfinance). Optional settings live in `.env` (see `.env.example`; never commit it): `ANTHROPIC_API_KEY`
(only the explicit AI-agent CLI commands use it) and the `ATP_ALERT_*` variables for email alerts. The launcher loads `.env` for the app only
(existing environment variables win). For CLI commands in a terminal: `set -a; source .env; set +a`. The app shows whether each is configured and never
shows a value. Paper-run settings are validated JSON "profiles" in `data/app/paper_profiles/`, optionally saved from the Paper Trading page for CLI use (not required to run).

**5. First run, step by step.**

- *Setup & System Status* shows what is missing and how to fix it. A fresh install has no experiments, so most pages show an honest empty state.
- Create experiments with `python scripts/run_experiment.py` (historical backtests; the app does not launch them).
- Choose a paper account (an experiment with a saved portfolio) in the sidebar. Nothing is selected or created for you.
- *Paper Trading* -> choose strategy, paper account, instrument and timeframe. No account yet? Use *Create a new empty paper account*, then *Initialise paper account* (explicit, once per experiment).
- **Run paper cycle** performs a fresh preflight and one cycle in one click (*Preflight only (read-only)* is still available). Outcomes: NO_ACTION (a valid result: no signal), RISK_REJECTED,
  BLOCKED, AWAITING_APPROVAL (a proposed trade; no order yet), RECONCILIATION_REQUIRED, FAILED. Approve or reject the exact proposal on the *Approvals* tab; only an approval can produce ORDER_FILLED
  (a *simulated* paper fill). Inspect any cycle in *Cycle history & trace*.
- *Portfolio & Execution*: the stored portfolio, an explicit **Value open positions now** (equity is undefined if any open position has no price),
  paper intents, fills, fees and reconciliation. *Monitoring & Alerts*: runs, per-check status (unknown is never shown as healthy), findings, deliveries.

**6. What is running.** Only the app server. There is no background paper trading (one cycle per click, or `paper run --once`; trades need your approval), no monitoring unless you
start `monitor-scheduler` in a terminal (the app reports a running scheduler from its lock file, and never starts or stops one), no live prices until you
ask, and nothing keeps running after you stop the app. Alert delivery works only if you enabled and verified it (`alerts test-send`).

**7. Where things are stored.** `data/experiments.db` (experiments and portfolios), `data/paper/paper_runtime.db` (paper cycles, intents, fills,
reconciliations), `data/monitoring/monitoring.db` (runs, findings, alerts, scheduler attempts), `data/app/paper_profiles/` (profiles), `logs/`.

**8. Troubleshooting.** *Dependencies missing / wrong Python*: re-run `./scripts/setup_app.sh` (`PYTHON=python3.12 ./scripts/setup_app.sh` to choose one).
*Registry or paper database not found*: the app never creates them; create experiments with `scripts/run_experiment.py`, initialise a paper account on the Paper Trading
page. *Preflight fails or is blocked*: the failing check names the cause; fix it and re-run. *Port in use*: `--port 8502`. *Alerts invalid*: check the `ATP_ALERT_*` values.
Validate everything without starting the server: `.venv/bin/python -m src.dashboard.launch --check`. Real-machine validation: `python scripts/app_validation.py`.

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

## Paper trading (Sprint 18)

Paper-only, bounded, explicit target. Config is a JSON file with `"execution_mode": "paper"`, `experiment_id`, `strategy_name`, `symbol`,
`risk`, `stop`, `execution` (see `src/paper/config.py`). Nothing is created implicitly; `init` records the baseline.

```bash
python -m src.cli paper init --config paper.json --label baseline
python -m src.cli paper preflight --config paper.json
python -m src.cli paper run --once --config paper.json
python -m src.cli paper reconcile --config paper.json
python -m src.cli paper status --config paper.json --cycles --intents --discrepancies --fills
python scripts/paper_runtime_validation.py
```

Preflight prints PASS / FAIL / BLOCKED / NOT_EVALUABLE per check. One cycle per closed daily bar is idempotent (re-running replays the record);
an interrupted cycle is recovered at the next run by ledger lookup, never blind resubmission. Reconciliation mismatches are recorded and block
new risk; they are never auto-corrected. PaperBroker fills instantly and fully at the signal-bar close with configured slippage/fee: no partial
fills, intraday bars, NEXT_BAR_OPEN or shorts. Fixture validation proves plumbing; real-data runs may produce no signal (and then no fill).
There is no live adapter and no AI in this path.

### Guided flow (Sprint 20)

Open the app on **Home**: it shows the selected paper account, what needs attention, a setup checklist (required vs optional) and one suggested next step. Your last-used
account is remembered (`data/app/preferences.json`) and restored after a refresh; nothing is created or run by selecting it. On **Paper Trading**, *Run paper cycle* performs a fresh
preflight and one cycle in a single click. If the strategy finds a risk-approved trade, it becomes a **proposal** -- no order exists until you approve that exact proposal on the
*Approvals* tab (it expires after 30 minutes; rejecting or ignoring it places nothing). CLI equivalents: `paper proposals`, `paper approve --proposal ID`, `paper reject --proposal ID`.
**Activity** shows a read-only timeline of cycles, proposals, orders, fills, reconciliations, monitoring and alerts. Paper only; fills are simulated.
