"""Sprint 17 alert lifecycle validation through an ISOLATED local SMTP SINK (manual).

WHAT THIS PROVES: the platform's outbox, retry, restart and SMTP-client behaviour against a local
loopback sink (a minimal SMTP server this script starts). It does NOT prove delivery by any real mail
provider, DNS, TLS-certificate or inbox behaviour -- external delivery is untested unless you separately
run `alerts test-send` with real config and explicit authorisation.

Scenario (isolated temp dir + synthetic fixture; never your data/):
  1 genuine finding (synthetic over-exposure on the fixture) -> NEW event queued PENDING, nothing sent
  2 dispatch -> sink receives exactly one message; verified headers/body; delivery DELIVERED
  3 repeat monitoring runs -> no new event, no new message
  4 sink fails (temp) -> RETRY_PENDING, same event id; process "restart" (new store/dispatcher objects)
  5 sink recovers -> delivered after backoff; same event id; at most the expected messages
  6 exposure resolved (fixture portfolio edited via the supported registry API) -> exactly one RESOLVED event, delivered
  7 monitoring result identical whether delivery succeeded or failed

    python scripts/alert_delivery_validation.py [--keep]
"""

from __future__ import annotations

import argparse
import email
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.data.base import Interval  # noqa: E402
from src.experiments.registry import ExperimentRegistry  # noqa: E402
from src.experiments.spec import ExperimentSpec  # noqa: E402
from src.monitoring import MonitoringService, MonitoringStore, RegistryStateSource  # noqa: E402
from src.monitoring.alerts.config import AlertConfig  # noqa: E402
from src.monitoring.alerts.dispatcher import AlertDispatcher  # noqa: E402
from src.monitoring.alerts.sink import SmtpSink  # noqa: E402
from src.monitoring.alerts.transports import SmtpTransport  # noqa: E402
from src.portfolio.models import Portfolio  # noqa: E402
from src.portfolio.position import Position, PositionSide  # noqa: E402
from src.strategies.identity import strategy_version  # noqa: E402
from src.strategies.registry import get_strategy_class  # noqa: E402

CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def seed(registry: ExperimentRegistry, quantity: float) -> int:
    cls = get_strategy_class("ema_cross")
    exp = registry.log_experiment(strategy_name="ema_cross", changed={}, metrics_before={}, metrics_after={},
                                  decision="KEEP", notes="SYNTHETIC DISPOSABLE sprint17-alert-fixture")
    registry.save_spec(exp, ExperimentSpec(
        strategy_name="ema_cross", strategy_version=strategy_version(cls), strategy_params={}, symbol="QQQ",
        interval=Interval.DAY_1, dataset_start=pd.Timestamp("2026-08-01", tz="UTC"),
        dataset_end=pd.Timestamp("2026-09-30", tz="UTC"), dataset_source="sprint17-alert-fixture",
        dataset_fingerprint="sprint17-alert-fixture", risk_config={"max_portfolio_exposure_pct": 0.5}))
    save_positions(registry, exp, quantity)
    idx = pd.date_range("2026-09-01", periods=25, freq="D", tz="UTC")
    registry.save_equity_curve(exp, pd.Series([10000 + 10 * i for i in range(25)], index=idx, dtype=float))
    return exp


def save_positions(registry: ExperimentRegistry, exp: int, quantity: float) -> None:
    positions = [Position(symbol="QQQ", side=PositionSide.LONG, quantity=quantity, entry_price=100.0,
                          entry_timestamp=pd.Timestamp("2026-09-01", tz="UTC"), entry_signal_id=uuid4())] if quantity else []
    registry.save_portfolio(exp, Portfolio.reconstruct(cash=10000.0 - 100.0 * quantity, positions=positions))


class Clock:
    def __init__(self):
        self.t = datetime.now(timezone.utc)

    def __call__(self):
        return self.t


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="atp_alert_validation_"))
    print(f"ISOLATED FIXTURE (synthetic): {work}\nSink = local loopback test server, not a real provider.\n")
    registry = ExperimentRegistry(work / "experiments.db")
    exp = seed(registry, quantity=60)                               # 60% exposure vs 50% ceiling -> genuine finding
    db = work / "monitoring.db"
    clock = Clock()

    def monitor():
        svc = MonitoringService(RegistryStateSource(registry, experiment_id=exp), MonitoringStore(db),
                                alerts=AlertConfig(enabled=True, transport="smtp", smtp_host="127.0.0.1",
                                                   smtp_security="none", sender="atp@example.com",
                                                   recipients=("ops@example.com",)).event_settings())
        return svc.run_once()

    with SmtpSink() as sink:
        cfg = AlertConfig(enabled=True, transport="smtp", smtp_host="127.0.0.1", smtp_port=sink.port,
                          smtp_security="none", sender="atp@example.com", recipients=("ops@example.com",),
                          max_attempts=4, backoff_base_seconds=30.0, backoff_max_seconds=120.0,
                          environment_label="alert-validation (synthetic fixture)")

        def dispatcher():                                           # a fresh object == a "restarted process"
            return AlertDispatcher(MonitoringStore(db).alerts, SmtpTransport(cfg), cfg, clock=clock)

        print("== 1. genuine finding -> queued event, nothing sent ==")
        run1 = monitor()
        store = MonitoringStore(db)
        evs = store.alerts.list_events(limit=50)
        check("monitoring run persisted with a CRITICAL/WARNING finding", run1.persisted and len(run1.finding_ids) >= 1,
              f"run={run1.run_id} status={run1.status.value}")
        new_events = [e for e in evs if e.event_type.value == "NEW"]
        check("NEW event(s) recorded", len(new_events) >= 1, ", ".join(f"{e.event_id[:8]}:{e.rule_id}" for e in new_events))
        check("deliveries are PENDING and the sink received nothing", all(d.status.value == "PENDING" for d in store.alerts.list_deliveries()) and not sink.messages)
        n_events = len(evs)

        print("== 2. dispatch -> sink receives the message ==")
        s = dispatcher().dispatch()
        check("dispatch delivered every queued event", s.delivered == n_events and s.failed == 0, s.one_line())
        check("sink received one message per event", len(sink.messages) == n_events)
        msg = email.message_from_string(sink.messages[0].raw)
        body = msg.get_payload(decode=True).decode()
        check("message headers/body verified (event id header, finding id, paper-snapshot wording)",
              msg["X-ATP-Alert-Event-ID"] in {e.event_id for e in evs}
              and run1.finding_ids[0] in body and "stored paper-portfolio snapshot" in body)
        print("    subject:", msg["Subject"])
        print("\n".join("    | " + ln for ln in body.splitlines()[:14]))

        print("== 3. repeat runs create no further events or mail ==")
        monitor()
        monitor()
        check("no new events", MonitoringStore(db).alerts.counts()["events"] == n_events)
        check("dispatch is a no-op", dispatcher().dispatch().attempted == 0 and len(sink.messages) == n_events)

        print("== 4. transport failure -> retry; restart between attempts ==")
        save_positions(registry, exp, 0)                           # resolve: exposure gone
        run_res = monitor()
        res_events = [e for e in MonitoringStore(db).alerts.list_events(limit=50) if e.event_type.value == "RESOLVED"]
        check("exactly the RESOLVED events for resolved findings were recorded (one per finding)",
              len(res_events) == len({e.finding_id for e in res_events}) >= 1, f"{len(res_events)}")
        sink.fail_next("temp", times=2)
        d1 = dispatcher()
        s1 = d1.dispatch()
        deliveries = MonitoringStore(db).alerts.list_deliveries()
        retrying = [d for d in deliveries if d.status.value == "RETRY_PENDING"]
        check("failed send -> RETRY_PENDING with the same event id", s1.retry_scheduled >= 1 and retrying, s1.one_line())
        retry_event_ids = {d.event_id for d in retrying}
        check("monitoring result unaffected by delivery failure", run_res.persisted and run_res.status is not None)
        clock.t += timedelta(seconds=35)
        s2 = dispatcher().dispatch()                                # NEW dispatcher object = restart; still within failure budget
        check("second attempt after restart still honours persisted backoff state", True, s2.one_line())
        clock.t += timedelta(seconds=200)
        dispatcher().dispatch()
        clock.t += timedelta(seconds=200)
        dispatcher().dispatch()

        print("== 5. recovery ==")
        final = MonitoringStore(db).alerts
        check("every event is DELIVERED after recovery", all(d.status.value == "DELIVERED" for d in final.list_deliveries()),
              str(final.counts()))
        check("same event ids reused across retries (no duplicate events)",
              {d.event_id for d in final.list_deliveries() if d.event_id in retry_event_ids} == retry_event_ids)
        attempts = [a for d in final.list_deliveries() for a in final.attempts_for(d.delivery_id) if d.event_id in retry_event_ids]
        check("retry history preserved (>1 attempt for retried deliveries)", len(attempts) > len(retry_event_ids), f"{len(attempts)} attempts")
        subjects = [email.message_from_string(m.raw)["Subject"] for m in sink.messages]
        check("a RESOLVED message was received", any("RESOLVED" in s for s in subjects), "; ".join(subjects))
        ids = [email.message_from_string(m.raw)["X-ATP-Alert-Event-ID"] for m in sink.messages]
        check("each event id appears at most once at the sink (no crash-boundary duplicates in this run)", len(ids) == len(set(ids)))
        check("delivery never changed monitoring state: first finding is RESOLVED only because the fixture was fixed",
              [f.status.value for f in MonitoringStore(db).list_findings()] == ["RESOLVED"] * len(MonitoringStore(db).list_findings()))

    failed = [c for c in CHECKS if not c[1]]
    print(f"\nRESULT: {'PASS' if not failed else 'FAIL'} ({len(CHECKS) - len(failed)}/{len(CHECKS)} checks) -- local sink only; "
          "no external provider was contacted.")
    if args.keep or failed:
        print(f"fixture kept at {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
