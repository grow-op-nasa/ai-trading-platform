"""The controlled candidate execution harness -- src/ai/agents/strategy_dev/_harness.py.

Sprint 14 spec, sections 8-9, 29: this script is **platform-owned
infrastructure**, not an agent capability -- the agent never sees this
file's path, never invokes a shell tool, and has no way to alter what
this script does. `runner.py` is the only caller, via a subprocess with
a sanitized environment and a hard timeout.

This is the *only* place candidate source is ever actually executed.
Everything it does is deliberately narrow:

    argv: <source_path> <candles_json_path> <symbol> <params_json>
        |
        v
    load candidate module from source_path (already statically
    validated by src.ai.agents.strategy_dev.safety before this script
    is ever invoked)
        |
        v
    instantiate the one candidate class with symbol + params
        |
        v
    strategy.prepare(candles) -> strategy.generate_signals(prepared)
        |
        v
    print one JSON object to stdout: {"signals": [...], "error": null}

Never reads environment variables beyond what Python itself needs;
`runner.py` launches this with a minimal, secret-free environment. Never
imports anything beyond what candidate source itself is statically
permitted to import (`safety.ALLOWED_*`) plus the small amount of
harness plumbing below (json, sys, importlib -- used only by this
platform-owned script, never exposed to candidate code as an import
target it could reach).
"""

from __future__ import annotations

import importlib.util
import json
import sys


def _load_candles(path: str):
    import pandas as pd

    with open(path, "r", encoding="utf-8") as fh:
        records = json.load(fh)
    frame = pd.DataFrame.from_records(records)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.set_index("timestamp").sort_index()
    return frame


def _load_candidate_class(source_path: str):
    import ast

    with open(source_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)
    class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    if len(class_names) != 1:
        raise RuntimeError(f"expected exactly one class in candidate source, found {class_names}")

    spec = importlib.util.spec_from_file_location("candidate_strategy_module", source_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load candidate module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_names[0])


def _signal_to_dict(signal) -> dict:
    return {
        "timestamp": signal.timestamp.isoformat(),
        "symbol": signal.symbol,
        "direction": signal.direction.value,
        "confidence": signal.confidence,
        "metadata": {k: v for k, v in signal.metadata.items() if _json_safe(v)},
    }


def _json_safe(value) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(json.dumps({"signals": [], "error": "usage: harness.py <source> <candles> <symbol> <params>"}))
        return 1

    _, source_path, candles_path, symbol, params_json = argv
    try:
        params = json.loads(params_json)
        candidate_cls = _load_candidate_class(source_path)
        strategy = candidate_cls(symbol=symbol, **params)
        candles = _load_candles(candles_path)
        prepared = strategy.prepare(candles)
        signals = strategy.generate_signals(prepared)
        payload = {"signals": [_signal_to_dict(s) for s in signals], "error": None}
    except Exception as exc:  # noqa: BLE001 -- untrusted code, report don't propagate
        payload = {"signals": [], "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
