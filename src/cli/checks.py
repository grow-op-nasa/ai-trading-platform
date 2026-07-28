"""Individual `atp doctor` health checks.

Each function takes no arguments, does one focused check, and returns a
`CheckResult` -- it should never raise; `run_doctor()` treats a raised
exception as a bug in the check itself, not a healthy/unhealthy signal.
Import order here is display order in `atp doctor`'s output.
"""

from __future__ import annotations

import sys

import pandas as pd

from src.cli.registry import CheckResult, CheckStatus, register_check
from src.config import settings
from src.data.exceptions import DataProviderError
from src.data.service import DEFAULT_CACHE_DIR, MarketDataService
from src.experiments.registry import DEFAULT_DB_PATH, ExperimentRegistry
from src.utils.cache import CacheManager

MIN_PYTHON_VERSION = (3, 12)

_DOCTOR_PROBE_KEY = "__atp_doctor_probe__"


@register_check("Python Version")
def check_python_version() -> CheckResult:
    """Running interpreter meets the Sprint 0 minimum (3.12+)."""
    current = sys.version_info[:2]
    detail = sys.version.split()[0]
    if current >= MIN_PYTHON_VERSION:
        return CheckResult("Python Version", CheckStatus.OK, detail)
    required = ".".join(map(str, MIN_PYTHON_VERSION))
    return CheckResult(
        "Python Version", CheckStatus.FAIL, f"{detail} < required {required}+"
    )


@register_check("Configuration")
def check_configuration() -> CheckResult:
    """Settings module loads, required directories exist, watchlist is
    non-empty. Deliberately structural -- ADR-0005's typed Settings
    object (not yet built) would let this check validation logic too."""
    try:
        missing = [
            str(d) for d in (settings.DATA_DIR, settings.LOG_DIR) if not d.exists()
        ]
        if missing:
            return CheckResult(
                "Configuration", CheckStatus.FAIL, f"missing directories: {missing}"
            )
        if not settings.WATCHLIST:
            return CheckResult("Configuration", CheckStatus.FAIL, "WATCHLIST is empty")
        return CheckResult(
            "Configuration", CheckStatus.OK, f"watchlist: {settings.WATCHLIST}"
        )
    except Exception as exc:  # settings failing to import/construct at all
        return CheckResult("Configuration", CheckStatus.FAIL, str(exc))


@register_check("Market Data")
def check_market_data() -> CheckResult:
    """Can actually reach the configured data provider right now.

    Deliberately bypasses the cache (`use_cache=False`) -- this check's
    job is to catch "the vendor is unreachable/changed," which a cache
    hit would silently mask. Cache health itself is a separate check.
    """
    symbol = settings.WATCHLIST[0] if settings.WATCHLIST else "SPY"
    try:
        service = MarketDataService(use_cache=False)
        candles = service.get_history(symbol, period="5d")
        if candles is None or candles.empty:
            return CheckResult(
                "Market Data", CheckStatus.FAIL, f"no data returned for {symbol}"
            )
        return CheckResult("Market Data", CheckStatus.OK, f"{len(candles)} candles ({symbol})")
    except DataProviderError as exc:
        return CheckResult("Market Data", CheckStatus.FAIL, str(exc))
    except Exception as exc:
        return CheckResult("Market Data", CheckStatus.FAIL, f"unexpected error: {exc}")


@register_check("Cache")
def check_cache() -> CheckResult:
    """Round-trips a throwaway DataFrame through the real cache
    directory, then deletes it. Verifies the cache is writable and
    readable, not just importable."""
    try:
        cache = CacheManager(DEFAULT_CACHE_DIR)
        probe = pd.DataFrame(
            {"value": [1, 2, 3]},
            index=pd.date_range("2024-01-01", periods=3, freq="D", name="timestamp"),
        )
        cache.set(_DOCTOR_PROBE_KEY, probe)
        round_tripped = cache.get(_DOCTOR_PROBE_KEY)
        path = cache._path_for(_DOCTOR_PROBE_KEY)
        if path.exists():
            path.unlink()

        if round_tripped is None or len(round_tripped) != len(probe):
            return CheckResult("Cache", CheckStatus.FAIL, "round-trip read did not match write")
        return CheckResult("Cache", CheckStatus.OK)
    except Exception as exc:
        return CheckResult("Cache", CheckStatus.FAIL, str(exc))


@register_check("Experiments DB")
def check_experiments_db() -> CheckResult:
    """Can open the real Experiment Registry SQLite file and query it.

    Note: this is the project's only database today, so it also covers
    what an earlier draft of this check list called "Database" -- there
    is no separate general-purpose database to check independently.
    """
    try:
        registry = ExperimentRegistry(db_path=DEFAULT_DB_PATH)
        count = registry.count()
        return CheckResult("Experiments DB", CheckStatus.OK, f"{count} experiment(s) recorded")
    except Exception as exc:
        return CheckResult("Experiments DB", CheckStatus.FAIL, str(exc))


@register_check("Broker Connection")
def check_broker_connection() -> CheckResult:
    """`src/broker/` doesn't exist yet -- broker connectivity is
    Sprint 5. Reported as NOT_IMPLEMENTED rather than skipped so the
    doctor's output is an honest, complete list of everything the
    system will eventually need to be healthy, not just what exists
    today."""
    return CheckResult(
        "Broker Connection",
        CheckStatus.NOT_IMPLEMENTED,
        "src/broker/ not built yet (Sprint 5)",
    )


@register_check("API Keys")
def check_api_keys() -> CheckResult:
    """The only data provider today (yfinance) requires no API key.
    Revisit when a keyed provider (Polygon, Interactive Brokers, a
    broker API, etc.) is added."""
    return CheckResult(
        "API Keys",
        CheckStatus.NOT_IMPLEMENTED,
        "current provider (yfinance) requires no key",
    )
