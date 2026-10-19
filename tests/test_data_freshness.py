"""Daily-data freshness regression tests (Sprint 22 follow-up: systematic DATA_STALE scan failures).

Root cause under test: `YFinanceProvider` passed the platform's INCLUSIVE `end` date to yfinance, whose `end` is EXCLUSIVE, so the
bar for the scan date was never requested and every weekday scan after the close saw the previous session as the latest bar.

Nothing here touches a network. A fake `yfinance` module reproduces the vendor's exclusive-`end` convention and a controllable
publication rule (which sessions the vendor has published, and whether it also returns the still-forming bar); it sits BEHIND the
real `YFinanceProvider`, `MarketDataService`, `NYSECalendar`, shared closed-bar preparation and the real scan engine. The
assertions check the selected completed-bar date and the final classification, not merely "fresh".
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.appsvc.paths import AppPaths
from src.appsvc.scanner import ScannerService
from src.calendar.nyse import NYSECalendar
from src.data.base import Interval
from src.data.exceptions import NoDataError
from src.data.service import MarketDataService
from src.data.yfinance_provider import YFinanceProvider, vendor_request_range
from src.paper.evaluation import prepare_bars
from src.scanner.models import ResultStatus

CAL = NYSECalendar()
UTC = timezone.utc
STRATEGY = "ema_cross"


def utc(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ fake vendor
class FakeVendor:
    """What the (faked) yfinance module serves. `published_through`: the last session the vendor has published a bar for.
    `forming`: also return a bar for `forming_day` (a session still in progress). `raw` overrides the response entirely."""

    def __init__(self, published_through: date, forming_day: date | None = None) -> None:
        self.published_through = published_through
        self.forming_day = forming_day
        self.raw: pd.DataFrame | None = None
        self.calls: list[tuple[str, date, date]] = []

    def frame(self, start: date, end_exclusive: date) -> pd.DataFrame:
        days = [d for d in CAL.sessions_between(start, end_exclusive - timedelta(days=1)) if d <= self.published_through]
        if self.forming_day is not None and start <= self.forming_day < end_exclusive and self.forming_day not in days:
            days.append(self.forming_day)
        idx = pd.DatetimeIndex([pd.Timestamp(d) for d in sorted(days)])          # daily bars: naive dates, as yfinance returns
        base = pd.Series([100.0 + 0.1 * i for i in range(len(idx))], index=idx)
        return pd.DataFrame({"Open": base, "High": base + 1, "Low": base - 1, "Close": base + 0.5, "Volume": 1_000_000.0}, index=idx)


class FakeYF:
    """Stands in for the `yfinance` module inside `src.data.yfinance_provider`."""

    def __init__(self, vendor: FakeVendor) -> None:
        self.vendor = vendor

    def Ticker(self, symbol):
        vendor = self.vendor

        class _T:
            def history(self, start, end, interval, auto_adjust):
                vendor.calls.append((symbol, start, end))
                if vendor.raw is not None:
                    return vendor.raw
                return vendor.frame(start, end)               # `end` is EXCLUSIVE, exactly like yfinance

        return _T()


@pytest.fixture
def vendor(monkeypatch):
    v = FakeVendor(published_through=date(2026, 10, 16))
    monkeypatch.setattr("src.data.yfinance_provider.yf", FakeYF(v))
    return v


class Clock:
    def __init__(self, now: datetime, step: timedelta = timedelta(0)) -> None:
        self.now, self.step, self._lock = now, step, threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            t = self.now
            self.now = self.now + self.step
            return t


def service(tmp_path: Path, clock) -> ScannerService:
    md = MarketDataService(provider=YFinanceProvider(), cache_dir=tmp_path / "cache", use_cache=False)
    return ScannerService(AppPaths.under(tmp_path), market_data=md, clock=clock)


def scan(tmp_path: Path, now: datetime, symbols="QQQ SPY", step: timedelta = timedelta(0)):
    svc = service(tmp_path, Clock(now, step))
    wl = svc.create_watchlist("fresh", symbols)
    rec, _ = svc.scan(wl.watchlist_id, STRATEGY, {})
    return svc, svc.view(rec.scan_id)


def first(view):
    return view.results[0]


EVALUATED = {ResultStatus.NO_SIGNAL, ResultStatus.SIGNAL_FOUND, ResultStatus.RISK_BLOCKED}


# ================================================================================ the defect itself
def test_the_platform_end_date_is_inclusive_and_translated_for_yfinance():
    assert vendor_request_range(date(2026, 10, 19), date(2026, 10, 19)) == (date(2026, 10, 19), date(2026, 10, 20))
    assert vendor_request_range(date(2026, 1, 1), date(2026, 10, 16)) == (date(2026, 1, 1), date(2026, 10, 17))


def test_a_one_day_inclusive_request_returns_that_days_bar_instead_of_no_data(vendor):
    vendor.published_through = date(2026, 10, 19)
    df = YFinanceProvider().fetch_candles("QQQ", date(2026, 10, 19), date(2026, 10, 19), Interval.DAY_1)
    assert [ts.date() for ts in df.index] == [date(2026, 10, 19)]
    assert vendor.calls[-1] == ("QQQ", date(2026, 10, 19), date(2026, 10, 20))        # vendor got an exclusive end one day later


def test_the_request_end_that_could_omit_the_latest_bar_is_one_day_after_the_scan_date(tmp_path, vendor):
    vendor.published_through = date(2026, 10, 19)
    _, view = scan(tmp_path, utc(2026, 10, 19, 21, 44), "QQQ")
    sym, start, end_excl = vendor.calls[-1]
    assert end_excl == date(2026, 10, 20) and start == date(2026, 10, 19) - timedelta(days=400)
    assert first(view).data["request"] == {"start": str(start), "end": "2026-10-19"}      # persisted for diagnosis


def test_the_observed_failure_is_reproduced_by_the_old_convention_and_gone_with_the_fix(tmp_path, vendor, monkeypatch):
    """Scan 352e05239e72474b: Monday 2026-10-19 21:44Z, session closed 20:00Z, vendor HAS published Monday's bar."""
    vendor.published_through = date(2026, 10, 19)
    monkeypatch.setattr("src.data.yfinance_provider.vendor_request_range", lambda s, e: (s, e))   # the old, buggy pass-through
    _, broken = scan(tmp_path / "old", utc(2026, 10, 19, 21, 44), "QQQ")
    assert first(broken).reason_code == "DATA_STALE" and first(broken).data["dataset_end"].startswith("2026-10-16")
    monkeypatch.undo()
    monkeypatch.setattr("src.data.yfinance_provider.yf", FakeYF(vendor))
    _, fixed = scan(tmp_path / "new", utc(2026, 10, 19, 21, 44), "QQQ")
    assert first(fixed).status in EVALUATED and first(fixed).decision_bar.startswith("2026-10-19")


# ================================================================================ session scenarios
@pytest.mark.parametrize("label, now, published, forming, decision, expected, dropped", [
    ("Monday before the open: Friday is the latest completed bar", utc(2026, 10, 19, 12, 0), date(2026, 10, 16), None, "2026-10-16", "2026-10-16", 0),
    ("Monday during the session: forming bar is dropped, Friday decides", utc(2026, 10, 19, 15, 0), date(2026, 10, 16), date(2026, 10, 19), "2026-10-16", "2026-10-16", 1),
    ("Monday just after the close with Monday published", utc(2026, 10, 19, 20, 5), date(2026, 10, 19), None, "2026-10-19", "2026-10-19", 0),
    ("Monday an hour and a half after the close with Monday published", utc(2026, 10, 19, 21, 44), date(2026, 10, 19), None, "2026-10-19", "2026-10-19", 0),
    ("Saturday", utc(2026, 10, 17, 15, 0), date(2026, 10, 16), None, "2026-10-16", "2026-10-16", 0),
    ("Sunday evening", utc(2026, 10, 18, 21, 25), date(2026, 10, 16), None, "2026-10-16", "2026-10-16", 0),
    ("Labor Day (exchange holiday): the prior Friday is legitimately latest", utc(2026, 9, 7, 20, 0), date(2026, 9, 4), None, "2026-09-04", "2026-09-04", 0),
    ("Thanksgiving (exchange holiday)", utc(2026, 11, 26, 21, 0), date(2026, 11, 25), None, "2026-11-25", "2026-11-25", 0),
    ("Tuesday 00:30Z is still Monday evening in New York (UTC date != trading date)", utc(2026, 10, 20, 0, 30), date(2026, 10, 19), None, "2026-10-19", "2026-10-19", 0),
])
def test_valid_completed_data_is_accepted_with_the_right_decision_bar(tmp_path, vendor, label, now, published, forming, decision, expected, dropped):
    vendor.published_through, vendor.forming_day = published, forming
    _, view = scan(tmp_path, now, "QQQ")
    r = first(view)
    assert r.status in EVALUATED, (label, r.reason_code, r.reason)
    assert r.decision_bar.startswith(decision) and r.data["expected_session"] == expected
    assert r.data["dropped_incomplete"] == dropped
    assert r.data["evaluated_window"]["last_bar"].startswith(decision)               # the strategy never saw a later bar


def test_a_daily_bar_stamped_at_utc_midnight_is_the_trading_date_not_the_previous_new_york_evening(tmp_path, vendor):
    vendor.published_through = date(2026, 10, 19)
    _, view = scan(tmp_path, utc(2026, 10, 19, 21, 0), "QQQ")
    ts = pd.Timestamp(first(view).decision_bar)
    assert ts == pd.Timestamp("2026-10-19", tz="UTC") and ts.tz_convert("America/New_York").date() == date(2026, 10, 18)
    assert first(view).data["expected_session"] == "2026-10-19"                       # decided as Monday, not Sunday


# ================================================================================ genuine staleness is still reported
def test_after_the_close_but_inside_the_grace_the_missing_bar_is_awaiting_not_stale_and_never_no_signal(tmp_path, vendor):
    vendor.published_through = date(2026, 10, 16)                                     # Monday not published yet
    _, view = scan(tmp_path, utc(2026, 10, 19, 20, 30), "QQQ")
    r = first(view)
    assert r.status is ResultStatus.NOT_EVALUABLE and r.reason_code == "AWAITING_PROVIDER_BAR"
    assert r.decision_bar.startswith("2026-10-16") and r.data["expected_session"] == "2026-10-19"
    assert view.scan.status.value == "FAILED"                                          # nothing was evaluated: reported, not green


def test_past_the_grace_with_the_bar_still_unpublished_is_data_stale_with_an_explicit_reason(tmp_path, vendor):
    vendor.published_through = date(2026, 10, 16)
    _, view = scan(tmp_path, utc(2026, 10, 19, 21, 44), "QQQ")
    r = first(view)
    assert r.status is ResultStatus.NOT_EVALUABLE and r.reason_code == "DATA_STALE"
    assert "latest complete bar is 2026-10-16, expected 2026-10-19" in r.reason
    assert r.status not in EVALUATED


def test_a_week_old_dataset_is_stale_whatever_the_weekday(tmp_path, vendor):
    vendor.published_through = date(2026, 10, 9)
    _, view = scan(tmp_path, utc(2026, 10, 19, 21, 44), "QQQ")
    r = first(view)
    assert r.reason_code == "DATA_STALE" and r.decision_bar.startswith("2026-10-09") and r.data["expected_session"] == "2026-10-19"


def test_missing_data_is_never_converted_into_no_signal(tmp_path, vendor):
    vendor.raw = pd.DataFrame()
    _, view = scan(tmp_path, utc(2026, 10, 19, 21, 44), "QQQ")
    r = first(view)
    assert r.status is ResultStatus.DATA_UNAVAILABLE and r.status not in EVALUATED


def test_a_malformed_vendor_response_is_not_evaluated(tmp_path, vendor):
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-15"), pd.Timestamp("2026-10-16")])
    vendor.raw = pd.DataFrame({"Close": [1.0, 2.0]}, index=idx)                      # required columns missing
    _, view = scan(tmp_path, utc(2026, 10, 19, 12, 0), "QQQ")
    r = first(view)
    assert r.status not in EVALUATED and r.status is not ResultStatus.NO_SIGNAL


# ================================================================================ early close (calendar limitation)
def test_early_close_is_not_modelled_so_the_day_is_treated_conservatively_never_as_a_false_fresh_bar(tmp_path, vendor):
    """NYSE closes at 13:00 ET on 2026-11-27; `NYSECalendar` models 16:00 ET only. Until 16:00 ET the bar is dropped as
    incomplete and the prior session decides -- late, but it can never admit a bar the calendar does not call complete."""
    vendor.published_through = date(2026, 11, 27)
    _, early = scan(tmp_path / "a", utc(2026, 11, 27, 19, 0), "QQQ")                    # 14:00 ET: really closed, modelled open
    r = first(early)
    assert r.status in EVALUATED and r.decision_bar.startswith("2026-11-25") and r.data["dropped_incomplete"] == 1
    _, late = scan(tmp_path / "b", utc(2026, 11, 27, 21, 30), "QQQ")                    # 16:30 ET: complete under the model
    assert first(late).decision_bar.startswith("2026-11-27")


# ================================================================================ one reference time per scan
def test_every_symbol_in_a_scan_uses_the_same_as_of_even_when_the_clock_advances(tmp_path, vendor):
    vendor.published_through = date(2026, 10, 16)                                     # Monday never published
    # the clock starts 10 minutes inside the 60-minute grace and moves 5 minutes per reading: per-symbol readings would split
    # the symbols between AWAITING_PROVIDER_BAR and DATA_STALE
    _, view = scan(tmp_path, utc(2026, 10, 19, 20, 50), "QQQ SPY IWM DIA AAPL MSFT NVDA AMD", step=timedelta(minutes=5))
    assert len(view.results) == 8
    assert len({r.data["as_of"] for r in view.results}) == 1
    assert len({r.reason_code for r in view.results}) == 1


# ================================================================================ scanner / paper-evaluation agreement
@pytest.mark.parametrize("now, published, forming", [
    (utc(2026, 10, 19, 12, 0), date(2026, 10, 16), None),
    (utc(2026, 10, 19, 15, 0), date(2026, 10, 16), date(2026, 10, 19)),
    (utc(2026, 10, 19, 20, 30), date(2026, 10, 16), None),
    (utc(2026, 10, 19, 21, 44), date(2026, 10, 16), None),
    (utc(2026, 10, 19, 21, 44), date(2026, 10, 19), None),
    (utc(2026, 10, 18, 21, 25), date(2026, 10, 16), None),
])
def test_the_scanner_and_the_shared_paper_evaluation_classify_identical_data_identically(tmp_path, vendor, now, published, forming):
    vendor.published_through, vendor.forming_day = published, forming
    _, view = scan(tmp_path, now, "QQQ")
    r = first(view)
    md = MarketDataService(provider=YFinanceProvider(), cache_dir=tmp_path / "c2", use_cache=False)
    ds = md.get_dataset("QQQ", start=now.date() - timedelta(days=400), end=now.date(), interval=Interval.DAY_1, use_cache=False)
    prep = prepare_bars(ds.candles, CAL, now)
    assert prep.kind == ("OK" if r.status in EVALUATED else r.reason_code)
    assert str(prep.decision_bar) == r.decision_bar


# ================================================================================ replay guarantees preserved
def test_a_replayed_stale_result_is_reproduced_as_data_but_never_a_successful_evaluation(tmp_path, vendor):
    vendor.published_through = date(2026, 10, 16)
    svc, view = scan(tmp_path, utc(2026, 10, 19, 21, 44), "QQQ")
    vendor.calls.clear()
    rep = svc.replay(view.scan.scan_id)
    sym = rep.results[0]
    assert first(view).reason_code == "DATA_STALE" and sym.overall == "REPRODUCED"
    assert sym.strategy.status == "NOT_APPLICABLE" and sym.risk.status == "NOT_APPLICABLE"      # nothing was evaluated, nothing is claimed
    assert vendor.calls == []                                                                 # replay is offline
    again = svc.view(view.scan.scan_id)
    assert again.scan.status.value == "FAILED" and again.results[0].reason_code == "DATA_STALE"    # the stored record is untouched


def test_a_stale_scan_creates_no_paper_state_orders_intents_or_proposals(tmp_path, vendor):
    vendor.published_through = date(2026, 10, 16)
    scan(tmp_path, utc(2026, 10, 19, 21, 44), "QQQ")
    created = sorted(p.name for p in tmp_path.rglob("*") if p.is_file() and "paper" in str(p.relative_to(tmp_path)))
    assert created == []                                                                      # the scanner never touches the paper store


def test_provider_no_data_still_raises_for_an_empty_inclusive_range_with_nothing_published(vendor):
    vendor.published_through = date(2026, 1, 1)
    with pytest.raises(NoDataError):
        YFinanceProvider().fetch_candles("QQQ", date(2026, 10, 19), date(2026, 10, 19), Interval.DAY_1)
