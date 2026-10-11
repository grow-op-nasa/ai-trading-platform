"""Approved-strategy package -- src/strategies/promoted/ (ADR-0049).

The one controlled place where a human-approved candidate becomes a
production strategy. The package contains:

    manifest.json     -- the explicit allow-list of promoted strategies
    <name>.py         -- each approved candidate's source, **verbatim**
                         (its SHA-256 equals the candidate's recorded
                         `source_hash`)

Nothing else is ever loaded from this directory. `load_promoted_
strategies()` imports exactly the modules the manifest names, and only
after proving each file still hashes to the manifest's `source_hash` --
a tampered or swapped file fails closed (that strategy is simply not
registered, and the failure is reported in the returned `LoadReport`).
This is deliberately *not* generic filesystem discovery of arbitrary
code into `StrategyRegistry`.

The manifest is written only by `src.promotion` (the human-operated
promotion service). It is source-controlled: promotion changes show up
as an ordinary reviewable diff. Nothing under `src.ai.agents` may import
this package's writer functions or `src.promotion`.

Importing this package registers every manifest-listed strategy with
`src.strategies.registry` as a side effect, exactly like the
hand-written strategies in `src/strategies/__init__.py`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from src.strategies.registry import register_strategy

PROMOTED_DIR = Path(__file__).resolve().parent
MANIFEST_NAME = "manifest.json"
MODULE_PREFIX = "src.strategies.promoted"

__all__ = [
    "PROMOTED_DIR",
    "MANIFEST_NAME",
    "PromotedEntry",
    "LoadReport",
    "read_manifest",
    "write_manifest",
    "load_module_class",
    "load_promoted_strategies",
    "LOAD_REPORT",
]


@dataclass(frozen=True)
class PromotedEntry:
    """One manifest record: a promoted strategy and its full lineage."""

    strategy_name: str
    module: str  # file stem inside this package
    class_name: str
    candidate_id: str
    source_hash: str
    spec_hash: str
    agent_run_id: str
    approved_at: str
    approved_by: str
    registered_at: str


@dataclass
class LoadReport:
    loaded: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def read_manifest(directory: Path | str = PROMOTED_DIR) -> list[PromotedEntry]:
    path = Path(directory) / MANIFEST_NAME
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [PromotedEntry(**e) for e in raw.get("entries", [])]


def write_manifest(entries: list[PromotedEntry], directory: Path | str = PROMOTED_DIR) -> None:
    """Atomically rewrite the manifest (temp file + `os.replace`)."""
    path = Path(directory) / MANIFEST_NAME
    payload = {"entries": [asdict(e) for e in entries], "schema_version": 1}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def load_module_class(directory: Path | str, entry: PromotedEntry, *, verify_hash: bool = True):
    """Import `entry.module` from `directory` and return its class.

    Raises:
        FileNotFoundError / ValueError / AttributeError: the file is
            missing, no longer hashes to `entry.source_hash`, or lacks
            `entry.class_name`.
    """
    path = Path(directory) / f"{entry.module}.py"
    if not path.is_file():
        raise FileNotFoundError(f"promoted module file missing: {path.name}")
    if verify_hash and _sha256_file(path) != entry.source_hash:
        raise ValueError(
            f"promoted module {path.name} no longer matches the manifest source_hash "
            f"(file changed after promotion)"
        )
    module_name = f"{MODULE_PREFIX}.{entry.module}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load promoted module {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return getattr(module, entry.class_name)


def load_promoted_strategies(
    directory: Path | str = PROMOTED_DIR,
    *,
    register: Callable[[str], Callable[[type], type]] = register_strategy,
) -> LoadReport:
    """Register every manifest-listed strategy. Fail closed per entry:
    one bad entry is reported in `LoadReport.errors` and skipped; it
    never prevents the platform (or other entries) from loading."""
    report = LoadReport()
    try:
        entries = read_manifest(directory)
    except (OSError, ValueError, TypeError) as exc:
        report.errors["<manifest>"] = f"{type(exc).__name__}: {exc}"
        return report
    for entry in entries:
        try:
            cls = load_module_class(directory, entry)
            register(entry.strategy_name)(cls)
            report.loaded.append(entry.strategy_name)
        except BaseException as exc:  # noqa: BLE001 -- fail closed, report
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            report.errors[entry.strategy_name] = f"{type(exc).__name__}: {exc}"
    return report


#: Result of the import-time load of the production manifest.
LOAD_REPORT: LoadReport = load_promoted_strategies()
