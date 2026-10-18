"""Sprint 22: the CI workflow is a quality gate, not a decoration. These tests pin its contract."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _on(doc: dict) -> dict:
    # PyYAML parses the bare key `on` as boolean True.
    return doc.get("on", doc.get(True))


def _run_lines(doc: dict) -> list[str]:
    return [s["run"] for job in doc["jobs"].values() for s in job["steps"] if "run" in s]


def test_triggers_are_pull_requests_and_pushes_to_main():
    on = _on(_load())
    assert on["pull_request"]["branches"] == ["main"]
    assert on["push"]["branches"] == ["main"]


def test_python_312_with_pip_cache():
    steps = [s for j in _load()["jobs"].values() for s in j["steps"]]
    setup = next(s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python"))
    assert setup["with"]["python-version"] == "3.12"
    assert setup["with"]["cache"] == "pip"


def test_required_gate_commands_present_in_order():
    runs = "\n".join(_run_lines(_load()))
    order = [
        "pip install -r requirements-dev.txt",
        "pytest -q",
        "compileall -q src scripts tests",
        "ruff check src scripts tests",
    ]
    positions = [runs.index(c) for c in order]
    assert positions == sorted(positions)


def test_no_failure_masking_and_no_secrets():
    text = WORKFLOW.read_text()
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "secrets." not in text


def test_only_self_contained_validation_scripts_run():
    allowed = {"paper_runtime_validation.py --skip-external", "alert_delivery_validation.py"}
    for run in _run_lines(_load()):
        if "scripts/" in run and "validation" in run:
            assert any(a in run for a in allowed), run
    # these touch live providers or the real network
    text = WORKFLOW.read_text()
    for forbidden in ("app_validation", "scanner_validation", "scanner_audit", "data_hash_probe",
                      "monitoring_live", "monitoring_agent_live", "scheduler_validation"):
        assert forbidden not in text


def test_dev_requirements_pin_ruff_exactly_and_include_runtime():
    lines = [ln.strip() for ln in (REPO / "requirements-dev.txt").read_text().splitlines()]
    assert "-r requirements.txt" in lines
    ruff = [ln for ln in lines if ln.startswith("ruff")]
    assert len(ruff) == 1 and "==" in ruff[0]


def test_ruff_config_has_no_blanket_exclusions():
    import tomllib

    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["ruff"]
    assert "exclude" not in cfg and "extend-exclude" not in cfg
    lint = cfg["lint"]
    assert "ignore" not in lint and "per-file-ignores" not in lint
