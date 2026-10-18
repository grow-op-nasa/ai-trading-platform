"""Sprint 22: the paper runtime honours the strategy-declared history requirement (shared contract, ADR-0059)."""

from __future__ import annotations


from tests.test_paper_runtime import STRAT, UndeclaredStrategy as Undeclared, World, _reset_script  # noqa: F401  (autouse fixture)
from src.paper.models import CycleStatus


def _checks(rt):
    return {c.name: c for c in rt.preflight(check_data=False).checks}


def test_insufficient_history_blocks_the_cycle_and_never_looks_like_no_signal(tmp_path):
    w = World(tmp_path, config_over={"strategy_params": {"history_bars": 4000}})
    rec = w.runtime().run_cycle()
    assert rec.status is CycleStatus.BLOCKED and rec.reason_code == "INSUFFICIENT_HISTORY"
    assert rec.dataset["history"]["satisfied"] is False and rec.dataset["history"]["required"] == 4000
    assert rec.fill is None and rec.intent_id is None


def test_a_satisfied_requirement_is_recorded_with_the_cycle(tmp_path):
    w = World(tmp_path, config_over={"strategy_params": {"history_bars": 40}})
    rec = w.runtime().run_cycle()
    assert rec.status is not CycleStatus.BLOCKED
    h = rec.dataset["history"]
    assert h["satisfied"] is True and h["required"] == 40 and h["available"] >= 40 and h["status"] == "DECLARED"


def test_preflight_fails_when_the_lookback_can_never_hold_the_requirement(tmp_path):
    w = World(tmp_path, config_over={"strategy_params": {"history_bars": 4000}})
    check = _checks(w.runtime())["strategy_history_requirement"]
    assert check.status.name in ("FAIL", "BLOCKED") and "lookback_days" in check.detail


def test_preflight_passes_with_an_explained_requirement(tmp_path):
    w = World(tmp_path)
    check = _checks(w.runtime())["strategy_history_requirement"]
    assert check.status.name == "PASS" and "30" in check.detail


def test_an_undeclared_registered_strategy_cannot_run_in_the_paper_runtime(tmp_path):
    from src.strategies.registry import _unregister_strategy, register_strategy
    register_strategy("paper_undeclared_test")(Undeclared)
    try:
        w = World(tmp_path, config_over={"strategy_name": "paper_undeclared_test"})
        assert _checks(w.runtime())["strategy_history_requirement"].status.name == "FAIL"
        rec = w.runtime().run_cycle()
        assert rec.status is CycleStatus.BLOCKED and rec.reason_code == "HISTORY_REQUIREMENT_UNKNOWN"
    finally:
        _unregister_strategy("paper_undeclared_test")
    assert STRAT


# ------------------------------------------------------------------ snapshot reference on paper cycles
def test_a_paper_cycle_preserves_the_dataset_it_decided_on(tmp_path):
    from src.snapshots import SnapshotStore
    from src.utils.hashing import dataframe_fingerprint

    w = World(tmp_path)
    store = SnapshotStore(tmp_path / "snaps")
    rec = w.runtime(snapshots=store).run_cycle()
    ref = rec.dataset["snapshot"]
    assert ref["status"] == "CAPTURED"
    loaded = store.load(ref["snapshot_id"])
    assert dataframe_fingerprint(loaded.candles) == rec.dataset["dataset_hash"]


def test_a_paper_cycle_whose_snapshot_cannot_be_stored_is_blocked_before_any_intent(tmp_path):
    class Broken:
        def put(self, *a, **k):
            raise OSError("disk full")

    w = World(tmp_path)
    w.script(__import__("tests.test_paper_runtime", fromlist=["WED"]).WED, "LONG")
    rec = w.runtime(snapshots=Broken()).run_cycle()
    assert rec.status is CycleStatus.BLOCKED and rec.reason_code == "SNAPSHOT_FAILED"
    assert w.store().list_intents(w.exp) == [] and w.store().list_fills(w.exp) == []


def test_without_a_snapshot_store_the_cycle_says_so(tmp_path):
    rec = World(tmp_path).runtime().run_cycle()
    assert rec.dataset["snapshot"] == {"status": "NOT_CAPTURED"}
