"""Minimal `.env` loader (Sprint 19). Reads KEY=VALUE lines into a mapping; never overrides an existing variable and
never prints a value. It does not execute anything (no shell expansion)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import MutableMapping

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not _KEY.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def load_env_file(path: Path | str, environ: MutableMapping[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Returns (names loaded, warnings). Existing environment variables always win. Values are never returned."""
    environ = os.environ if environ is None else environ
    p = Path(path)
    if not p.is_file():
        return [], []
    warnings: list[str] = []
    try:
        if p.stat().st_mode & 0o077:
            warnings.append(f"{p.name} is readable by other users; run: chmod 600 {p.name}")
        values = parse_env_text(p.read_text())
    except OSError as exc:
        return [], [f"could not read {p.name}: {type(exc).__name__}"]
    loaded = []
    for k, v in values.items():
        if k not in environ:
            environ[k] = v
            loaded.append(k)
    return loaded, warnings
