"""`atp doctor` -- a full system health check.

Runs every check registered in `src/cli/checks.py` (via
`src/cli/registry.py`) and prints a status line per check, so a failure
six months from now names exactly which subsystem broke instead of
requiring a guess. Usage:

    python -m src.cli doctor
"""

from __future__ import annotations

# Importing src.cli.checks triggers every @register_check call as a
# side effect -- this import must happen before run_doctor() reads the
# registry, and is the only place that needs to know the check module
# exists.
from src.cli import checks  # noqa: F401
from src.cli.registry import CheckResult, CheckStatus, registered_checks

_SYMBOLS = {
    CheckStatus.OK: "✓",
    CheckStatus.FAIL: "✗",
    CheckStatus.NOT_IMPLEMENTED: "—",
}


def run_doctor() -> int:
    """Run all registered checks, print the report, return an exit code.

    Returns:
        0 if every check passed or was NOT_IMPLEMENTED, 1 if any check
        failed.
    """
    results = _run_all_checks()
    print(_format_report(results))
    return 1 if any(r.status is CheckStatus.FAIL for r in results) else 0


def _run_all_checks() -> list[CheckResult]:
    results = []
    for name, func in registered_checks().items():
        try:
            results.append(func())
        except Exception as exc:
            # A check that crashes is itself a failed check, not a
            # crashed doctor command.
            results.append(
                CheckResult(name, CheckStatus.FAIL, f"check raised {exc!r}")
            )
    return results


def _format_report(results: list[CheckResult]) -> str:
    if not results:
        return "No health checks are registered."

    name_width = max(len(r.name) for r in results) + 4
    lines = []
    for r in results:
        line = f"{r.name:<{name_width}}{_SYMBOLS[r.status]}"
        if r.detail and r.status is not CheckStatus.OK:
            line += f"   ({r.detail})"
        lines.append(line)

    failures = [r for r in results if r.status is CheckStatus.FAIL]
    not_implemented = [r for r in results if r.status is CheckStatus.NOT_IMPLEMENTED]

    lines.append("")
    if failures:
        names = ", ".join(r.name for r in failures)
        lines.append(f"{len(failures)} check(s) failed: {names}")
    elif not_implemented:
        names = ", ".join(r.name for r in not_implemented)
        lines.append(f"Everything Healthy ({len(not_implemented)} not yet implemented: {names})")
    else:
        lines.append("Everything Healthy")

    return "\n".join(lines)
