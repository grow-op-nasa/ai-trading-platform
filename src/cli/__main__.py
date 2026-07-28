"""Entry point for `python -m src.cli <command>`.

Only `doctor` exists today. New subcommands register themselves in
COMMANDS the same way new health checks register themselves in
`src/cli/registry.py` -- adding one is a plug-in, not a rewrite.
"""

from __future__ import annotations

import sys
from typing import Callable

from src.cli.doctor import run_doctor

COMMANDS: dict[str, Callable[[], int]] = {
    "doctor": run_doctor,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        available = ", ".join(COMMANDS)
        print(f"usage: python -m src.cli {{{available}}}")
        return 2
    return COMMANDS[argv[0]]()


if __name__ == "__main__":
    raise SystemExit(main())
