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


def _run_strategy_dev_agent(argv: list[str]) -> int:
    # Imported lazily, not at module load time: `src.cli.strategy_dev`
    # pulls in the full agent/tool stack (`ResearchTrialService` ->
    # `ModelRegistry` -> `joblib`), which `strategy-promote` and
    # `doctor` have no business depending on just because they live in
    # the same `ARGV_COMMANDS` dict.
    from src.cli.strategy_dev import run_strategy_dev_agent

    return run_strategy_dev_agent(argv)


def _run_strategy_promote(argv: list[str]) -> int:
    # Imported lazily for the same reason, and also so that this
    # deliberately agent-decoupled, human-only command (Sprint 14,
    # ADR-0047, sections 92-93) never shares an import path with
    # anything in `src.ai.agents.strategy_dev.tools`/`dev_agent`.
    from src.cli.strategy_promote import run_strategy_promote

    return run_strategy_promote(argv)


# Commands that take the remaining argv themselves, rather than being
# zero-argument callables -- kept as a separate mapping so `doctor`'s
# existing zero-argument contract (and every test against it) is
# unaffected by this addition.
#
# `strategy-promote` (Sprint 14, ADR-0047) is deliberately listed here
# and nowhere in `src.ai.agents.strategy_dev` -- it is a human-only
# command, never an agent tool.
ARGV_COMMANDS: dict[str, Callable[[list[str]], int]] = {
    "research-agent": run_research_agent,
    "strategy-dev": _run_strategy_dev_agent,
    "strategy-promote": _run_strategy_promote,
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
