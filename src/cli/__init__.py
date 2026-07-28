"""The `atp` command-line tool.

Today this is invoked as `python -m src.cli <command>` (e.g. `python -m
src.cli doctor`), not as a bare `atp` shell command -- wiring a real
global `atp` entry point requires the packaging work already tracked in
`DECISIONS.md` ADR-0004 (the `src/ai_trading_platform/` rename +
`pyproject.toml` console-script metadata), which is deliberately
deferred to pre-1.0. See ADR-0013 for why the command is scoped this
way for now.
"""
