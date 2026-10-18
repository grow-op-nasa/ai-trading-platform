"""Scan execution: one registered strategy over a watchlist, daily bars, read-only.

Everything that decides a symbol's outcome is the SAME code the paper runtime uses (`src.paper.evaluation`): production
registry resolution, closed-bar/freshness preparation, the strategy's real output at the decision bar and the existing
risk engine. This module only fans out, isolates failures, orders results and records provenance. It imports no broker,
execution-submission, intent or proposal code and never writes outside the scanner store.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from src.data.exceptions import DataProviderError, DataValidationError
from src.paper.evaluation import (StrategyResolutionError, assess_risk, evaluate_at_bar, prepare_bars, resolve_strategy,
                                  safe_error_text, signal_to_record)
from src.scanner.models import (DEFAULT_CONCURRENCY, MAX_CONCURRENCY, ResultStatus, RiskState, ScanRecord, ScanResult,
                                ScanStatus, Watchlist)
from src.scanner.store import ScannerStore

STALE_HEARTBEAT_SECONDS = 180
MIN_BARS_FLOOR = 60


class ScanSetupError(RuntimeError):
    """The scan could not be defined (unknown strategy, empty watchlist, unsupported interval...). Nothing was recorded."""


@dataclass
class RiskContext:
    """Read-only inputs for the existing risk interface. `portfolio` is the account's SAVED portfolio; it is copied per
    symbol and never written back. Each candidate is assessed alone, not as part of a basket (documented caveat)."""
    risk: Any
    stop_policy: Any
    portfolio: Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_PERIOD_NAME = re.compile(r"(^|_)(fast|slow|period|window|lookback|length|span|signal|bars|days)($|_)")
MIN_BARS_CAP = 200      # a 400-calendar-day lookback yields ~275 sessions; a larger requirement could never be met


def scanner_min_bars(params: dict[str, Any]) -> int:
    """Scanner POLICY (strategies declare no lookback, so this is a heuristic): at least 60 complete bars, or 3x the largest
    positive integer parameter whose NAME looks like a period/window (fast, slow, period, window, lookback, length, span,
    signal, bars, days, e.g. `atr_period`), capped at 200 so it stays satisfiable within the default lookback. Integer
    thresholds such as `overbought=70` are not periods and are ignored. A strategy needing more history than this must
    declare it itself (not supported yet)."""
    periods = [v for k, v in params.items()
               if isinstance(v, int) and not isinstance(v, bool) and v > 0 and _PERIOD_NAME.search(str(k).lower())]
    return min(MIN_BARS_CAP, max(MIN_BARS_FLOOR, 3 * max(periods, default=0)))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


_ACTIVE: set[str] = set()       # scans executing in THIS process
_ACTIVE_LOCK = threading.Lock()


class ScanEngine:
    def __init__(self, store: ScannerStore, market_data: Any, *, calendar: Any = None, clock: Callable[[], datetime] = _utc_now,
                 concurrency: int = DEFAULT_CONCURRENCY, allowed_strategy_prefixes: tuple[str, ...] = ("src.strategies",)) -> None:
        if calendar is None:
            from src.calendar.nyse import NYSECalendar
            calendar = NYSECalendar()
        self._store, self._md, self._cal, self._clock = store, market_data, calendar, clock
        self._conc = max(1, min(int(concurrency), MAX_CONCURRENCY))
        self._prefixes = allowed_strategy_prefixes

    # ------------------------------------------------------------------ recovery
    def _is_stale(self, rec: ScanRecord) -> bool:
        with _ACTIVE_LOCK:
            if rec.scan_id in _ACTIVE:
                return False
        if rec.owner_pid is not None and rec.owner_pid != os.getpid() and _pid_alive(rec.owner_pid):
            try:
                beat = datetime.fromisoformat(rec.heartbeat_at or rec.started_at)
            except ValueError:
                return True
            return (self._clock() - beat).total_seconds() > STALE_HEARTBEAT_SECONDS
        return True

    def effective_status(self, rec: ScanRecord) -> ScanStatus:
        """Read-only: a RUNNING record whose owner is gone is shown as INTERRUPTED without writing anything."""
        if rec.status is ScanStatus.RUNNING and self._is_stale(rec):
            return ScanStatus.INTERRUPTED
        return rec.status

    def recover_interrupted(self) -> int:
        n = 0
        for rec in self._store.running():
            if self._is_stale(rec):
                self._store.mark_interrupted(rec.scan_id, self._counts(self._store.results(rec.scan_id)))
                n += 1
        return n

    # ------------------------------------------------------------------ define + start
    def start(self, watchlist: Watchlist, strategy_name: str, params: dict[str, Any] | None = None, *, interval: str = "1d",
              lookback_days: int = 400, account: int | None = None, risk_enabled: bool = False,
              request_token: str | None = None) -> tuple[ScanRecord, bool]:
        """Create the immutable scan record (status RUNNING). Returns (record, created). If an identical scan is already
        RUNNING, or the request token was already used, the EXISTING scan is returned and nothing new is created."""
        params = dict(params or {})
        if interval != "1d":
            raise ScanSetupError("the scanner supports daily bars only")
        if not watchlist.symbols:
            raise ScanSetupError("the watchlist has no symbols")
        try:
            _, shash = resolve_strategy(strategy_name, watchlist.symbols[0], params, self._prefixes)
        except StrategyResolutionError as exc:
            raise ScanSetupError(str(exc)) from exc
        min_bars = scanner_min_bars(params)
        risk_mode = "ASSESSED" if (risk_enabled and account is not None) else "NOT_ASSESSED"
        ident = {"symbols": list(watchlist.symbols), "strategy": strategy_name, "strategy_hash": shash, "params": params,
                 "interval": interval, "lookback_days": lookback_days, "min_bars": min_bars, "account": account if risk_mode == "ASSESSED" else None,
                 "risk_mode": risk_mode}
        chash = hashlib.sha256(json.dumps(ident, sort_keys=True, default=str).encode()).hexdigest()[:16]
        self.recover_interrupted()
        for r in self._store.running():
            if r.config_hash == chash and not self._is_stale(r):
                return r, False
        now = self._clock().isoformat(timespec="seconds")
        rec = ScanRecord(scan_id=uuid.uuid4().hex[:16], status=ScanStatus.RUNNING, watchlist_id=watchlist.watchlist_id,
                         watchlist_name=watchlist.name, symbols=tuple(watchlist.symbols), strategy_name=strategy_name,
                         strategy_hash=shash, strategy_params=params, config_hash=chash, interval=interval,
                         lookback_days=lookback_days, account=ident["account"], risk_mode=risk_mode, started_at=now,
                         counts={}, request_token=request_token, owner_pid=os.getpid(), heartbeat_at=now,
                         min_bars=min_bars, concurrency=self._conc)
        rec, created = self._store.create_scan(rec)
        if created:
            with _ACTIVE_LOCK:
                _ACTIVE.add(rec.scan_id)
        return rec, created

    # ------------------------------------------------------------------ execute
    def execute(self, rec: ScanRecord, *, risk: RiskContext | None = None,
                progress: Callable[[int, int, str], None] | None = None, note: str | None = None) -> ScanRecord:
        with _ACTIVE_LOCK:
            _ACTIVE.add(rec.scan_id)
        total, done = len(rec.symbols), 0
        try:
            def work(pos_sym: tuple[int, str]) -> ScanResult:
                pos, sym = pos_sym
                try:
                    res = self._evaluate(rec, sym, pos, risk)
                except Exception as exc:  # noqa: BLE001 - one symbol can never abort the scan
                    res = self._result(rec, sym, pos, ResultStatus.ERROR, "UNEXPECTED_ERROR",
                                       "An unexpected error occurred while evaluating this symbol.", error=safe_error_text(exc))
                self._store.add_result(res)
                return res
            with ThreadPoolExecutor(max_workers=self._conc) as pool:
                futures = [pool.submit(work, ps) for ps in enumerate(rec.symbols)]
                for fut in as_completed(futures):           # progress is reported from the CALLING thread (UI-safe)
                    res = fut.result()
                    done += 1
                    if progress:
                        try:
                            progress(done, total, res.symbol)
                        except Exception:  # noqa: BLE001 - a UI callback must not break the scan
                            pass
                    if done % 5 == 0:
                        self._store.heartbeat(rec.scan_id)
            results = self._store.results(rec.scan_id)
            counts = self._counts(results)
            evaluated = sum(1 for r in results if r.status.evaluated)
            if len(results) < total:
                status, err = ScanStatus.PARTIAL, "some symbols were not recorded"
            elif evaluated == 0:
                status, err = ScanStatus.FAILED, "no symbol could be evaluated"
            elif evaluated < total:
                status, err = ScanStatus.PARTIAL, None
            else:
                status, err = ScanStatus.COMPLETED, None
            return self._store.finish_scan(rec.scan_id, status, counts, err or note)
        except BaseException as exc:
            self._store.finish_scan(rec.scan_id, ScanStatus.FAILED, self._counts(self._store.results(rec.scan_id)), safe_error_text(exc))
            raise
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(rec.scan_id)

    @staticmethod
    def _counts(results: list[ScanResult]) -> dict[str, int]:
        c = {s.value: 0 for s in ResultStatus}
        for r in results:
            c[r.status.value] += 1
        c["TOTAL"] = len(results)
        return c

    # ------------------------------------------------------------------ one symbol
    def _result(self, rec: ScanRecord, sym: str, pos: int, status: ResultStatus, code: str, reason: str, **kw) -> ScanResult:
        return ScanResult(scan_id=rec.scan_id, symbol=sym, status=status, reason_code=code, reason=reason, position=pos,
                          evaluated_at=self._clock().isoformat(timespec="seconds"), **kw)

    def _evaluate(self, rec: ScanRecord, sym: str, pos: int, risk: RiskContext | None) -> ScanResult:
        R = ResultStatus
        try:
            instance, _ = resolve_strategy(rec.strategy_name, sym, rec.strategy_params, self._prefixes)
        except StrategyResolutionError as exc:
            return self._result(rec, sym, pos, R.ERROR, "STRATEGY_UNAVAILABLE", "The strategy could not be built for this symbol.",
                                error=safe_error_text(exc))
        now = self._clock()
        end = now.date()
        start = end - timedelta(days=rec.lookback_days)
        try:
            ds = self._md.get_dataset(sym, start=start, end=end, interval=_interval(rec.interval), use_cache=False)
        except DataValidationError as exc:
            return self._result(rec, sym, pos, R.NOT_EVALUABLE, "DATA_INVALID",
                                "Data was returned but failed the integrity checks, so it was not used.", error=safe_error_text(exc))
        except DataProviderError as exc:
            return self._result(rec, sym, pos, R.DATA_UNAVAILABLE, "DATA_UNAVAILABLE",
                                "The data provider returned no usable data for this symbol.", error=safe_error_text(exc))
        data = {"provider": getattr(ds, "provider", None), "content_hash": getattr(ds, "content_hash", None),
                "bars": int(len(ds.candles)), "dataset_end": str(getattr(ds, "dataset_end", ""))}
        prep = prepare_bars(ds.candles, self._cal, now)
        data.update({"expected_session": str(prep.expected_session) if prep.expected_session is not None else None,
                     "dropped_incomplete": len(prep.dropped or [])})
        if prep.kind != "OK":
            status = R.NOT_EVALUABLE
            text = {"NO_COMPLETE_BAR": "No complete daily bar is available.",
                    "CALENDAR_UNSUPPORTED": "The exchange calendar cannot confirm the latest session.",
                    "DATA_STALE": "The latest complete bar is older than the expected session; stale data is never evaluated.",
                    "AWAITING_PROVIDER_BAR": "The latest session has closed but the provider has not published its bar yet."}[prep.kind]
            return self._result(rec, sym, pos, status, prep.kind, f"{text} {prep.detail}".strip(), data=data,
                                decision_bar=str(prep.decision_bar) if prep.decision_bar is not None else None)
        data["complete_bars"] = int(len(prep.complete))
        db = str(prep.decision_bar)
        if len(prep.complete) < rec.min_bars:
            return self._result(rec, sym, pos, R.NOT_EVALUABLE, "INSUFFICIENT_HISTORY",
                                f"Only {len(prep.complete)} complete bars; the scanner requires {rec.min_bars} for these parameters.",
                                data=data, decision_bar=db)
        ev = evaluate_at_bar(instance, prep.complete, prep.decision_bar, sym)
        if ev.outcome == "STRATEGY_ERROR":
            return self._result(rec, sym, pos, R.ERROR, "STRATEGY_ERROR", "The strategy raised an error on this data.",
                                data=data, decision_bar=db, error=ev.detail)
        if ev.outcome in ("SYMBOL_MISMATCH", "AMBIGUOUS"):
            return self._result(rec, sym, pos, R.NOT_EVALUABLE, ev.outcome, ev.detail, data=data, decision_bar=db)
        if ev.outcome == "NO_SIGNAL":
            return self._result(rec, sym, pos, R.NO_SIGNAL, "NO_SIGNAL", "The strategy produced no signal at the latest completed bar.",
                                data=data, decision_bar=db, risk_state=RiskState.NOT_APPLICABLE)
        record = signal_to_record(ev.signal)
        if risk is None:
            return self._result(rec, sym, pos, R.SIGNAL_FOUND, "SIGNAL_FOUND", "The strategy produced a signal at the latest completed bar.",
                                data=data, decision_bar=db, decision=record, risk_state=RiskState.NOT_ASSESSED,
                                risk_detail="Risk was not assessed in this scan; the paper workflow's preflight decides.")
        price = float(prep.complete.loc[prep.decision_bar, "close"])
        re = assess_risk(risk.risk, risk.stop_policy, ev.signal, prep.complete, prep.decision_bar, copy.deepcopy(risk.portfolio), price)
        detail_extra = "Assessed alone against the account's saved portfolio, not as a basket."
        if re.kind == "APPROVED":
            return self._result(rec, sym, pos, R.SIGNAL_FOUND, "SIGNAL_FOUND", "Signal found and the read-only risk check approved it.",
                                data=data, decision_bar=db, decision=record, risk_state=RiskState.APPROVED, risk_detail=detail_extra)
        if re.kind == "ALREADY_FLAT":
            return self._result(rec, sym, pos, R.NO_SIGNAL, "ALREADY_FLAT", "The strategy wants to be flat and the account already is.",
                                data=data, decision_bar=db, decision=record, risk_state=RiskState.NOT_APPLICABLE)
        if re.kind == "ENGINE_ERROR":
            return self._result(rec, sym, pos, R.SIGNAL_FOUND, "SIGNAL_FOUND",
                                "Signal found, but the risk check itself failed, so risk is NOT assessed.", data=data, decision_bar=db,
                                decision=record, risk_state=RiskState.NOT_ASSESSED, risk_detail=re.reason, error=re.reason)
        return self._result(rec, sym, pos, R.RISK_BLOCKED, re.code or re.kind, f"Signal found but blocked by risk: {re.reason}",
                            data=data, decision_bar=db, decision=record, risk_state=RiskState.REJECTED,
                            risk_detail=f"{re.reason} {detail_extra}")


def _interval(name: str):
    from src.data.base import Interval
    return Interval(name)
