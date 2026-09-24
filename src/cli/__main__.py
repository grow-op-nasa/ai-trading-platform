"""Entry point for `python -m src.cli <command>`.

`doctor` takes no arguments; `research-agent` (Sprint 13, `DECISIONS.md`
ADR-0046) takes its own argv (`--goal ...`) -- COMMANDS holds either a
zero-argument callable or one accepting the remaining argv, and `main()`
dispatches based on which `research-agent` needs. New subcommands
register themselves here the same way new health checks register
themselves in `src/cli/registry.py` -- adding one is a plug-in, not a
rewrite.
"""

from __future__ import annotations

import sys
from typing import Callable

from src.cli.doctor import run_doctor
from src.cli.research_agent import run_research_agent

COMMANDS: dict[str, Callable[[], int]] = {
    "doctor": run_doctor,
}

# Commands that take the remaining argv themselves, rather than being
# zero-argument callables -- kept as a separate mapping so `doctor`'s
# existing zero-argument contract (and every test against it) is
# unaffected by this addition.
ARGV_COMMANDS: dict[str, Callable[[list[str]], int]] = {
    "research-agent": run_research_agent,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        available = ", ".join(list(COMMANDS) + list(ARGV_COMMANDS))
        print(f"usage: python -m src.cli {{{available}}}")
        return 2
    command, rest = argv[0], argv[1:]
    if command in ARGV_COMMANDS:
        return ARGV_COMMANDS[command](rest)
    if command in COMMANDS:
        return COMMANDS[command]()
    available = ", ".join(list(COMMANDS) + list(ARGV_COMMANDS))
    print(f"usage: python -m src.cli {{{available}}}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
