"""Read-only daily-data freshness diagnostic (Sprint 22 follow-up).

Shows, for each symbol, the whole chain that decides whether a scan may evaluate it:

    scan time -> request range (platform and vendor conventions) -> latest bars returned -> expected completed session
              -> completed-bar selection -> freshness classification

It calls the real market-data provider (network), then uses the same shared closed-bar preparation the scanner and the paper
runtime use. It writes nothing: no scanner/paper/registry state, no snapshots, no intents, cycles, proposals or fills.

    python scripts/freshness_probe.py [SYMBOL ...]          (default: SPY QQQ)

Exit status: 0 every symbol OK; 1 any symbol not OK (the reason is printed); 2 usage error.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from src.calendar.nyse import NYSECalendar  # noqa: E402
from src.data.base import Interval  # noqa: E402
from src.data.exceptions import DataProviderError, DataValidationError  # noqa: E402
from src.data.service import MarketDataService  # noqa: E402
from src.data.yfinance_provider import vendor_request_range  # noqa: E402
from src.paper.evaluation import prepare_bars  # noqa: E402

LOOKBACK_DAYS = 400


def probe(symbol: str, now: datetime, md: MarketDataService, cal: NYSECalendar) -> bool:
    end = now.date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    v_start, v_end = vendor_request_range(start, end)
    print(f"\n== {symbol}")
    print(f"  scan time       {now.isoformat(timespec='seconds')}   ({now.astimezone(cal_tz(cal)).isoformat(timespec='seconds')})")
    print(f"  request         platform [{start} .. {end}] inclusive   ->   vendor [{v_start} .. {v_end}) exclusive")
    try:
        ds = md.get_dataset(symbol, start=start, end=end, interval=Interval.DAY_1, use_cache=False)
    except DataValidationError as exc:
        print(f"  RESULT          NOT OK: data failed integrity checks ({type(exc).__name__}: {exc})")
        return False
    except DataProviderError as exc:
        print(f"  RESULT          NOT OK: provider returned no usable data ({type(exc).__name__}: {exc})")
        return False
    tail = [str(pd.Timestamp(ts).date()) for ts in ds.candles.index[-3:]]
    print(f"  bars returned   {len(ds.candles)}; last three: {', '.join(tail)}")
    prep = prepare_bars(ds.candles, cal, now)
    dropped = [str(pd.Timestamp(ts).date()) for ts in (prep.dropped or [])]
    print(f"  expected session {prep.expected_session}   dropped as incomplete: {dropped or 'none'}")
    print(f"  decision bar    {pd.Timestamp(prep.decision_bar).date() if prep.decision_bar is not None else 'none'}")
    ok = prep.kind == "OK"
    print(f"  RESULT          {'OK: fresh completed data' if ok else 'NOT OK: ' + prep.kind + ' -- ' + prep.detail}")
    return ok


def cal_tz(cal: NYSECalendar):
    import zoneinfo
    return zoneinfo.ZoneInfo(cal.local_timezone)


def main(argv: list[str] | None = None) -> int:
    symbols = [s.upper() for s in (argv if argv is not None else sys.argv[1:])] or ["SPY", "QQQ"]
    if any(s.startswith("-") for s in symbols):
        print(__doc__)
        return 2
    now = datetime.now(timezone.utc)
    md = MarketDataService(use_cache=False)
    cal = NYSECalendar()
    results = [probe(s, now, md, cal) for s in symbols]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
