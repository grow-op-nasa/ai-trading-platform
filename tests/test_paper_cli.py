"""`paper` CLI (Sprint 18, ADR-0053): explicit commands, exit codes, JSON output, read-only inspection."""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.cli.__main__ import main as cli_main
from src.cli.paper import (EXIT_ATTENTION, EXIT_BLOCKED, EXIT_CONFIG, EXIT_FAILED, EXIT_OK, run_paper)
from src.data.fault_injection import Fault, FaultKind
from src.data.service import MarketDataService
from src.paper.faults import SimulatedCrash
from src.portfolio.models import Portfolio
from tests.test_paper_runtime import (NEXT_DAY_AFTER_CLOSE, WED, ScriptedStrategy, World,
                                      _reset_script, state_fp)  # noqa: F401  (autouse fixture must be in scope)

PREFIXES = ("src.strategies", "tests.test_paper_runtime", "test_paper_runtime")


class Cli:
    def __init__(self, world: World, init: bool = False) -> None:
        self.w = world
        self.cfg_path = world.tmp / "paper.json"
        self.cfg_path.write_text(json.dumps(world.cfg_dict))
        self.out: list[str] = []
        self.base = ["--config", str(self.cfg_path), "--registry", str(world.reg_path), "--paper-db", str(world.store_path)]

    def run(self, *argv: str, **kw) -> int:
        self.out.clear()
        md = MarketDataService(provider=self.w.provider, cache_dir=self.w.tmp / "cache", use_cache=False)
        return run_paper([argv[0], *self.base, *argv[1:]], market_data=md, monitor_factory=self.w.monitor,
                         clock=lambda: self.w.now[0], emit=self.out.append, allowed_strategy_prefixes=PREFIXES, **kw)

    @property
    def text(self) -> str:
        return "\n".join(self.out)

    def json(self) -> dict:
        return json.loads(self.text)

    def approve_pending(self) -> int:
        """Sprint 20: the CLI creates a PROPOSAL; a human approves the exact id. Returns the approve exit code."""
        assert self.run("proposals", "--json") == EXIT_OK
        pending = [p for p in self.json() if p["status"] == "PENDING"]
        assert len(pending) == 1
        return self.run("approve", "--proposal", pending[0]["proposal_id"], "--json")


@pytest.fixture
def cli(tmp_path):
    w = World(tmp_path, initialize=False)
    return Cli(w)


def test_init_is_explicit_idempotence_protected_and_creates_the_database_only_here(cli):
    assert not cli.w.store_path.exists()
    assert cli.run("init", "--label", "cli fixture") == EXIT_OK and cli.w.store_path.exists()
    assert cli.run("init", "--label", "again") == EXIT_CONFIG and "already exists" in cli.text


def test_missing_or_invalid_configuration_exits_2_and_changes_nothing(cli, tmp_path):
    assert run_paper(["preflight", "--config", str(tmp_path / "nope.json")], emit=cli.out.append) == EXIT_CONFIG
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**cli.w.cfg_dict, "execution_mode": "live"}))
    assert run_paper(["run", "--once", "--config", str(bad)], emit=cli.out.append) == EXIT_CONFIG
    assert "paper" in "\n".join(cli.out) and not cli.w.store_path.exists()
    assert run_paper([], emit=cli.out.append) == EXIT_CONFIG


def test_run_requires_once_and_never_loops(cli):
    cli.run("init", "--label", "x")
    assert cli.run("run") == EXIT_CONFIG and "--once is required" in cli.text


def test_preflight_exit_codes_and_json_are_truthful(cli):
    assert cli.run("preflight") == EXIT_CONFIG                      # no paper database yet -> FAIL
    cli.run("init", "--label", "x")
    before = state_fp(cli.w)
    assert cli.run("preflight", "--json") == EXIT_OK
    data = cli.json()
    assert data["ready"] is True and {c["status"] for c in data["checks"]} == {"PASS"}
    assert state_fp(cli.w) == before
    assert cli.run("preflight", "--no-data-check") == EXIT_ATTENTION   # NOT_EVALUABLE is not a PASS


def test_run_once_filled_cycle_json_is_explicit_that_the_fill_is_simulated(cli):
    cli.run("init", "--label", "x")
    cli.w.script(WED, "LONG")
    assert cli.run("run", "--once", "--json") == EXIT_ATTENTION                 # a proposal, not an order
    pending = cli.json()
    assert pending["status"] == "AWAITING_APPROVAL" and pending["filled"] is False and pending["client_order_id"] is None
    assert cli.w.store().list_fills(cli.w.exp) == []
    assert cli.approve_pending() == EXIT_OK
    data = cli.json()
    assert data["status"] == "ORDER_FILLED" and data["client_order_id"] and data["fill"]["side"] == "BUY"
    assert data["monitoring"]["status"] == "HEALTHY" and data["reconciliation_status"] == "MATCH"
    assert cli.run("run", "--once", "--json") == EXIT_OK and cli.json()["replayed"] is True


def test_no_action_and_risk_rejection_are_distinct_zero_exit_statuses_without_a_fill(cli):
    cli.run("init", "--label", "x")
    assert cli.run("run", "--once", "--json") == EXIT_OK
    d = cli.json()
    assert d["status"] == "NO_ACTION" and d["filled"] is False
    cli.w.script(date(2026, 10, 8), "SHORT")
    cli.w.advance(date(2026, 10, 8), NEXT_DAY_AFTER_CLOSE)
    assert cli.run("run", "--once", "--json") == EXIT_OK
    d = cli.json()
    assert d["status"] == "RISK_REJECTED" and d["filled"] is False and d["client_order_id"] is None


def test_exit_codes_distinguish_blocked_failed_and_attention(cli):
    cli.run("init", "--label", "x")
    cli.w.provider.set_script([Fault(FaultKind.TIMEOUT)])
    assert cli.run("run", "--once", "--json") == EXIT_BLOCKED and cli.json()["status"] == "BLOCKED"
    cli.w.provider.set_script([None])
    ScriptedStrategy.RAISE = True
    assert cli.run("run", "--once", "--json") == EXIT_FAILED and cli.json()["reason_code"] == "STRATEGY_ERROR"
    ScriptedStrategy.RAISE = False
    # a tampered portfolio -> reconciliation required (needs an executed fill first)
    cli.w.script(WED, "LONG")
    assert cli.run("run", "--once") == EXIT_ATTENTION and cli.approve_pending() == EXIT_OK
    good = cli.w.portfolio()
    cli.w.registry.save_portfolio(cli.w.exp, Portfolio.reconstruct(cash=good.cash + 50, positions=list(good.positions.values())))
    cli.w.script(date(2026, 10, 8), "FLAT")
    cli.w.advance(date(2026, 10, 8), NEXT_DAY_AFTER_CLOSE)
    assert cli.run("run", "--once", "--json") == EXIT_ATTENTION
    assert cli.json()["status"] == "RECONCILIATION_REQUIRED"
    assert cli.run("reconcile", "--json") == EXIT_ATTENTION and cli.json()["status"] == "MISMATCH"


def test_status_inspects_cycles_intents_fills_and_discrepancies_read_only(cli):
    cli.run("init", "--label", "x")
    cli.w.script(WED, "LONG")
    cli.run("run", "--once")
    cli.approve_pending()
    before = state_fp(cli.w)
    assert cli.run("status", "--json") == EXIT_OK
    d = cli.json()
    assert d["cycles"][0]["status"] == "ORDER_FILLED" and d["intents"][0]["state"] == "APPLIED"
    assert d["unresolved_intents"] == [] and d["account"]["label"] == "x"
    assert cli.run("status", "--fills", "--json") == EXIT_OK and len(cli.json()["fills"]) == 1
    assert cli.run("status", "--discrepancies", "--json") == EXIT_OK and cli.json()["reconciliations"] == []
    assert cli.run("status", "--blocked") == EXIT_OK
    assert state_fp(cli.w) == before


def test_status_on_a_missing_database_fails_and_never_creates_it(cli):
    assert cli.run("status") == EXIT_CONFIG and not cli.w.store_path.exists()


def test_crash_then_status_shows_the_unresolved_intent_and_run_recovers_it(cli):
    cli.run("init", "--label", "x")
    cli.w.script(WED, "LONG")
    cli.w.faults.arm("broker_after_commit", SimulatedCrash("x"))
    with pytest.raises(SimulatedCrash):
        cli.w.runtime().run_cycle()                       # the CLI itself can never be given a fault object
    assert cli.run("status", "--intents", "--json") == EXIT_OK and len(cli.json()["unresolved_intents"]) == 1
    assert cli.run("run", "--once", "--json") == EXIT_OK and cli.json()["status"] == "ORDER_FILLED"
    assert cli.run("status", "--json") == EXIT_OK and cli.json()["unresolved_intents"] == []


def test_entry_point_dispatches_paper_lazily_and_usage_errors_exit_2(capsys=None):
    assert cli_main(["paper"]) == EXIT_CONFIG
    assert cli_main(["paper", "run"]) == EXIT_CONFIG          # --config is mandatory: no implicit target
