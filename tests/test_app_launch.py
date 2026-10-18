"""Local launch, setup scripts, `.env` handling and UI safety boundaries (Sprint 19, ADR-0054). Network-free."""

from __future__ import annotations

import ast
import os
import re
import socket
import stat
from pathlib import Path

import pytest

from src.config.envfile import load_env_file, parse_env_text
from src.dashboard import launch

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


# ------------------------------------------------------------------ launcher
@pytest.fixture
def ok_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                       # main() chdirs to the project root; this restores the cwd afterwards
    monkeypatch.setattr(launch, "check_environment", lambda version=None: [])


def test_default_bind_is_loopback_and_the_server_argv_says_so(ok_env):
    seen = []
    code = launch.main(["--no-browser", "--port", "0"], environ={}, run=lambda argv: seen.append(argv) or 0, emit=lambda s: None)
    assert code == 0 and seen
    argv = seen[0]
    assert argv[argv.index("--server.address") + 1] == "127.0.0.1"
    assert argv[argv.index("--browser.gatherUsageStats") + 1] == "false"
    assert "0.0.0.0" not in " ".join(argv)


@pytest.mark.parametrize("addr", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_a_non_loopback_bind_is_refused_unless_explicitly_allowed(ok_env, addr):
    out, ran = [], []
    assert launch.main(["--address", addr], environ={}, run=lambda a: ran.append(a) or 0, emit=out.append) == 2
    assert not ran and any("--allow-remote" in line for line in out)
    out2 = []
    assert launch.main(["--address", addr, "--allow-remote", "--check"], environ={}, emit=out2.append) == 0
    assert any("WARNING" in line and "exposes" in line for line in out2)


def test_check_mode_validates_and_starts_nothing(ok_env):
    ran = []
    out = []
    assert launch.main(["--check"], environ={}, run=lambda a: ran.append(a) or 0, emit=out.append) == 0
    assert not ran and any("nothing started" in line for line in out) and any("paper mode only" in line for line in out)


def test_startup_creates_no_database_or_data_directory(ok_env, tmp_path):
    launch.main(["--check"], environ={}, emit=lambda s: None)
    assert list(tmp_path.iterdir()) == []


def test_missing_requirements_are_reported_with_the_fix(monkeypatch):
    monkeypatch.setattr(launch.importlib.util, "find_spec", lambda name: None)
    problems = launch.check_environment((3, 12, 0))
    assert any("missing packages" in p and "setup_app.sh" in p for p in problems)
    assert any("Python 3.12+" in p for p in launch.check_environment((3, 10, 0)))


def test_workdir_selects_the_data_root_and_must_exist(ok_env, tmp_path):
    seen = []
    target = tmp_path / "isolated"
    out = []
    assert launch.main(["--workdir", str(target), "--check"], environ={}, emit=out.append) == 2 and not target.exists()
    target.mkdir()
    assert launch.main(["--workdir", str(target), "--no-browser", "--port", "0"], environ={},
                       run=lambda a: seen.append(os.getcwd()) or 0, emit=lambda s: None) == 0
    assert seen == [str(target.resolve())]


def test_a_busy_port_is_reported_not_crashed(ok_env):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); s.listen(1)
        port = s.getsockname()[1]
        out = []
        assert launch.main(["--port", str(port)], environ={}, run=lambda a: 0, emit=out.append) == 3
        assert any("already in use" in line for line in out)


def test_launcher_output_never_prints_env_values(ok_env, tmp_path):
    secret = "hunter" + "2-value"
    out = []
    launch.main(["--check"], environ={"ATP_ALERTS_ENABLED": "true", "ATP_ALERT_SMTP_PASSWORD": secret, "ATP_ALERT_SMTP_HOST": "h"}, emit=out.append)
    assert secret not in "\n".join(out)


# ------------------------------------------------------------------ .env handling
def test_env_parser_handles_quotes_comments_and_export():
    text = "# c\nA=1\nexport B='two words'\nC=\"x\"\nD=val # trailing\nbad line\n1X=no\n"
    assert parse_env_text(text) == {"A": "1", "B": "two words", "C": "x", "D": "val"}


def test_env_loader_never_overrides_existing_values_and_never_returns_them(tmp_path):
    p = tmp_path / ".env"
    p.write_text("KEEP=from_file\nNEW=fresh\n")
    p.chmod(0o600)
    env = {"KEEP": "from_shell"}
    loaded, warnings = load_env_file(p, env)
    assert env == {"KEEP": "from_shell", "NEW": "fresh"} and loaded == ["NEW"] and warnings == []
    assert "fresh" not in repr((loaded, warnings))


def test_env_loader_warns_about_a_world_readable_file_and_tolerates_absence(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n"); p.chmod(0o644)
    _, warnings = load_env_file(p, {})
    assert warnings and "chmod 600" in warnings[0]
    assert load_env_file(tmp_path / "missing", {}) == ([], [])


# ------------------------------------------------------------------ scripts, git hygiene
def test_setup_and_start_scripts_are_safe_and_executable():
    for name in ("setup_app.sh", "start_app.sh"):
        path = REPO / "scripts" / name
        text = path.read_text()
        assert path.stat().st_mode & stat.S_IXUSR, f"{name} must be executable"
        assert "set -euo pipefail" in text
        assert not re.search(r"\brm\s+-|git\s+(clean|reset|checkout|stash)|sqlite3|DROP\s", text), name
    setup = (REPO / "scripts" / "setup_app.sh").read_text()
    assert "requirements.txt" in setup and "cp .env.example .env" in setup and '[ ! -f .env ]' in setup
    assert "3, 12" in setup
    assert ".venv/bin/python" in (REPO / "scripts" / "start_app.sh").read_text()


def test_python_requirement_matches_the_project_declaration():
    assert re.search(r'requires-python\s*=\s*">=3\.12"', (REPO / "pyproject.toml").read_text())
    assert launch.MIN_PYTHON == (3, 12)


def test_secrets_and_local_data_are_git_ignored_and_the_example_holds_no_secret():
    ignore = (REPO / ".gitignore").read_text().splitlines()
    assert ".env" in ignore and ".env.*" in ignore and "!.env.example" in ignore and "data/*" in ignore and "logs/*" in ignore
    example = (REPO / ".env.example").read_text()
    for line in example.splitlines():
        if line.strip() and not line.startswith("#"):
            pytest.fail(f".env.example must have every setting commented out: {line!r}")
    assert not re.search(r"sk-ant-[A-Za-z0-9]{10,}", example)


# ------------------------------------------------------------------ UI safety boundaries (static)
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    return mods | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}


FORBIDDEN_FOR_UI = ("src.paper", "src.broker", "src.execution", "src.risk", "src.promotion", "src.ai", "anthropic", "subprocess",
                    "src.monitoring.alerts.transports")


def test_dashboard_reaches_the_platform_only_through_appsvc_and_analytics():
    offenders = {}
    for f in (SRC / "dashboard").glob("*.py"):
        bad = {m for m in _imports(f) if any(m == b or m.startswith(b + ".") for b in FORBIDDEN_FOR_UI)}
        if bad:
            offenders[f.name] = sorted(bad)
    assert offenders == {}


def test_appsvc_is_framework_free_and_cannot_reach_live_or_ai_code():
    offenders = {}
    for f in (SRC / "appsvc").glob("*.py"):
        bad = {m for m in _imports(f) if m.split(".")[0] in {"streamlit", "anthropic", "subprocess"} or m.startswith(
            ("src.dashboard", "src.broker", "src.promotion", "src.ai.agents.research", "src.ai.agents.monitoring", "src.ai.agents.strategy_dev.dev_agent"))}
        if bad:
            offenders[f.name] = sorted(bad)
    assert offenders == {}


def test_only_the_cli_and_appsvc_import_the_paper_runtime_and_nothing_imports_appsvc_below_the_ui():
    paper_importers, appsvc_importers = set(), set()
    for f in SRC.rglob("*.py"):
        rel = f.relative_to(SRC).parts[0]
        mods = _imports(f)
        if rel == "scanner":        # Sprint 21: the scanner may import ONLY the side-effect-free shared evaluation module
            assert {m for m in mods if m.startswith("src.paper")} <= {"src.paper.evaluation"}, f
            continue
        if rel not in ("paper", "cli", "appsvc") and any(m.startswith("src.paper") for m in mods):
            paper_importers.add(str(f.relative_to(REPO)))
        if rel not in ("appsvc", "dashboard") and any(m.startswith("src.appsvc") for m in mods):
            appsvc_importers.add(str(f.relative_to(REPO)))
    assert paper_importers == set() and appsvc_importers == set()


def test_only_appsvc_imports_the_scanner_and_the_dashboard_never_does():
    for f in SRC.rglob("*.py"):
        rel = f.relative_to(SRC).parts[0]
        if rel not in ("scanner", "appsvc") and any(m.startswith("src.scanner") for m in _imports(f)):
            raise AssertionError(f"{f.relative_to(REPO)} imports src.scanner; only src.appsvc may")


def test_no_ui_widget_offers_a_live_or_broker_choice_and_there_is_no_shell_or_exec():
    for f in (SRC / "dashboard").glob("*.py"):
        text = f.read_text()
        assert not re.search(r"(selectbox|radio|text_input|checkbox)\(\s*['\"][^'\"]*\b(live|broker)\b[^'\"]*['\"]", text, re.I), f.name
        assert not re.search(r"\b(os\.system|eval|exec)\(|import subprocess", text), f.name


def test_all_state_changing_ui_calls_sit_behind_a_button_or_form_submission():
    """Every service mutation in the pages is lexically inside an `if <button/form flag>:` branch."""
    mutating = ("run_cycle(", "initialize_account(", "save_profile(", "dispatch_pending(", "retry_failed(", "value_now(", ".preflight(",
               "run_with_preflight(", "approve_proposal(", "reject_proposal(", "create_paper_account(", "monitor_cycle(",
               ".scan(", "start_scan(", "run_scan(", ".replay(", "create_watchlist(", "update_watchlist(", "delete_watchlist(", "ensure_starter(")
    for f in [*(SRC / "dashboard").glob("page_*.py"), SRC / "dashboard" / "proposals_ui.py"]:
        lines = f.read_text().splitlines()
        for i, line in enumerate(lines):
            if any(m in line for m in mutating) and not line.lstrip().startswith(("#", "def ", '"""')):
                indent = len(line) - len(line.lstrip())
                guard = None
                for j in range(i - 1, -1, -1):
                    prev = lines[j]
                    pi = len(prev) - len(prev.lstrip())
                    if prev.strip() and pi < indent and (prev.lstrip().startswith(("if ", "elif ", "else", "try", "with ", "def "))):
                        if prev.lstrip().startswith(("if ", "elif ")) or "button" in prev:
                            guard = prev
                            break
                        indent = pi
                        if prev.lstrip().startswith("def "):
                            break
                assert guard is not None, f"{f.name}:{i + 1} calls a mutating service outside an explicit if-branch: {line.strip()}"


# ------------------------------------------------------------------ Sprint 20 (ADR-0056)
_MUTATORS = ("run_cycle", "run_with_preflight", "approve_proposal", "reject_proposal", "preflight", "initialize_account",
             "create_paper_account", "monitor_cycle", "value_now", "dispatch_pending", "retry_failed", "save_account",
             "scan", "start_scan", "run_scan", "create_watchlist", "update_watchlist", "delete_watchlist", "ensure_starter")


def _call_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "") for n in ast.walk(tree) if isinstance(n, ast.Call)}


def test_home_and_activity_pages_are_read_only_by_construction():
    for name in ("page_home.py", "page_activity.py"):
        assert _call_names(SRC / "dashboard" / name).isdisjoint(_MUTATORS), name


def test_the_read_only_homepage_and_timeline_services_cannot_reach_any_mutator():
    for name in ("home.py", "activity.py", "freshness.py", "explain.py"):
        assert _call_names(SRC / "appsvc" / name).isdisjoint(set(_MUTATORS) - {"save_account"}), name
        assert "save_account" not in _call_names(SRC / "appsvc" / name)


def test_only_the_approval_widget_module_can_approve_or_reject_a_proposal_in_the_ui():
    callers = {f.name for f in (SRC / "dashboard").glob("*.py") if {"approve_proposal", "reject_proposal"} & _call_names(f)}
    assert callers == {"proposals_ui.py"}


def test_no_ai_or_notification_code_can_approve_a_proposal():
    offenders = [str(f.relative_to(REPO)) for d in ("ai", "monitoring", "agents") if (SRC / d).is_dir() for f in (SRC / d).rglob("*.py")
                 if {"approve_proposal", "reject_proposal"} & _call_names(f)]
    assert offenders == []


def test_only_the_preference_store_writes_the_preference_file_and_the_dashboard_only_via_the_shell_callback():
    savers = {f.name for f in (SRC / "dashboard").glob("*.py") if "save_account" in _call_names(f)}
    assert savers == {"shell.py"}


def test_the_app_binds_to_loopback_by_default():
    text = (SRC / "dashboard" / "launch.py").read_text()
    assert "127.0.0.1" in text and "0.0.0.0" not in text
