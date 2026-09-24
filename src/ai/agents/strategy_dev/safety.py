"""Static safety validation for AI-generated candidate strategy source
-- src/ai/agents/strategy_dev/safety.py.

Sprint 14 spec, sections 9-20, 30, 95: this is the single most
safety-critical module in the whole package. Candidate source is
**untrusted** the moment an LLM produces it. Before any candidate can
be instantiated or executed (`runner.py`), it must pass every check
here:

    generated source
        |
        v
    size / complexity limits
        |
        v
    AST parse
        |
        v
    import allowlist            (default-deny, not a denylist)
        |
        v
    dynamic-execution denial     (eval/exec/compile/__import__/...)
        |
        v
    dangerous-construct scan     (dunder gadgets, forbidden calls)
        |
        v
    single-class / base-class check
        |
        v
    no self-registration check   (never @register_strategy)
        |
        v
    Strategy/Signal contract shape check (static, best-effort)

A candidate that fails *any* check must never execute -- `validate()`
never silently downgrades a failure to a warning (Sprint 14 spec,
section 26).

This is a **research-grade execution boundary, not a hardened security
sandbox for arbitrary hostile code** (Sprint 14 spec, section 30).
Python cannot be perfectly sandboxed from within a normal process by
AST inspection alone; this module is one layer of an explicit
defense-in-depth stack whose other layers are `runner.py`'s sanitized,
isolated subprocess execution, `workspace.py`'s filesystem boundary,
and the complete absence of any filesystem/shell/network tool from this
package's agent tool surface (`tools.py`). Do not present this module,
on its own, as a claim that arbitrary hostile code is safe to run.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

__all__ = [
    "ValidationResult",
    "validate_candidate_source",
    "ALLOWED_STDLIB_MODULES",
    "ALLOWED_THIRDPARTY_MODULES",
    "ALLOWED_PLATFORM_MODULES",
    "MAX_SOURCE_CHARS",
    "MAX_FUNCTIONS_PER_CLASS",
    "MAX_IMPORTS",
]

#: Stdlib modules a Strategy implementation might legitimately need.
#: Deliberately small -- Sprint 14 spec, section 10: "the allowlist
#: should be explicit," not "everything except a denylist."
ALLOWED_STDLIB_MODULES = frozenset(
    {
        "typing",
        "dataclasses",
        "enum",
        "math",
        "statistics",
        "__future__",
    }
)

#: Third-party libraries already used throughout the strategy layer.
ALLOWED_THIRDPARTY_MODULES = frozenset({"numpy", "pandas"})

#: Exact platform module paths a candidate may import from -- not a
#: prefix match against all of `src.*` (Sprint 14 spec, sections 14-18:
#: no broker/execution/portfolio/risk/data-service/dashboard/CLI/agent
#: access, statically enforced by simply never allowing those modules
#: to appear here at all).
ALLOWED_PLATFORM_MODULES = frozenset(
    {
        "src.signals.models",
        "src.strategies.base",
        "src.strategies.sdk",
        "src.indicators.engine",
        "src.regime.engine",
    }
)

#: Names that must never appear as a called function anywhere in
#: candidate source (Sprint 14 spec, section 11) -- dynamic code
#: execution / introspection escape hatches.
FORBIDDEN_CALL_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "open",
        "input",
        "breakpoint",
        "help",
    }
)

#: Attribute names that are the common Python sandbox-escape gadgets
#: (walking the object graph back to `object`, `type`, or a live
#: interpreter frame). Denied wherever they appear, not just when
#: obviously chained -- static analysis can't reliably tell "chained
#: for escape" from "chained by accident," so both are rejected.
FORBIDDEN_ATTRIBUTE_NAMES = frozenset(
    {
        "__subclasses__",
        "__bases__",
        "__mro__",
        "__globals__",
        "__builtins__",
        "__getattribute__",
        "__reduce__",
        "__reduce_ex__",
        "__class__",
        "__dict__",
        "__code__",
        "__closure__",
        "__loader__",
        "__spec__",
    }
)

#: A candidate must never import or reference this name -- Sprint 14
#: spec section 39/91: a candidate must never register itself into the
#: production `StrategyRegistry`.
FORBIDDEN_NAMES = frozenset({"register_strategy"})

MAX_SOURCE_CHARS = 20_000
MAX_FUNCTIONS_PER_CLASS = 20
MAX_IMPORTS = 15
MAX_CLASSES = 1

REQUIRED_BASE_NAME = "BaseStrategy"
REQUIRED_METHODS = frozenset({"prepare", "generate_signals"})


@dataclass(frozen=True)
class ValidationResult:
    """The structured outcome of `validate_candidate_source()` (Sprint
    14 spec, section 26) -- `passed` is `False` if `failed_checks` is
    non-empty, full stop; there is no partial-pass state.

    Args:
        passed: `True` only if `failed_checks` is empty.
        failed_checks: hard failures -- each one alone is sufficient to
            reject the candidate.
        warnings: non-fatal observations (e.g. "no docstring") that do
            not block validation but are surfaced to the researcher.
    """

    passed: bool
    failed_checks: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def validate_candidate_source(source: str) -> ValidationResult:
    """Run the complete static safety pipeline against `source`.

    Never raises for ordinary malformed/malicious input -- a syntax
    error, a forbidden import, a dynamic-exec attempt, and so on are
    all reported as `failed_checks`, not exceptions. This function may
    still raise for a genuine programming error in the validator
    itself (there is no legitimate "expected" exception path here).
    """
    failed: list[str] = []
    warnings: list[str] = []

    if not source or not source.strip():
        return ValidationResult(passed=False, failed_checks=("source is empty",))

    if len(source) > MAX_SOURCE_CHARS:
        failed.append(
            f"source exceeds the maximum size ({len(source)} > {MAX_SOURCE_CHARS} chars) "
            f"-- a strategy should be simple and reviewable, not an application framework"
        )

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return ValidationResult(passed=False, failed_checks=(f"syntax error: {exc}",))

    failed.extend(_check_imports(tree))
    failed.extend(_check_forbidden_calls(tree))
    failed.extend(_check_forbidden_attributes(tree))
    failed.extend(_check_forbidden_names(tree))
    class_failed, class_warnings = _check_class_shape(tree)
    failed.extend(class_failed)
    warnings.extend(class_warnings)

    return ValidationResult(passed=not failed, failed_checks=tuple(failed), warnings=tuple(warnings))


def _root_module(dotted: str) -> str:
    return dotted.split(".", 1)[0]


def _check_imports(tree: ast.AST) -> list[str]:
    failed: list[str] = []
    import_count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                import_count += 1
                module = alias.name
                if _root_module(module) in ("src",):
                    if module not in ALLOWED_PLATFORM_MODULES:
                        failed.append(f"import of {module!r} is not on the platform module allowlist")
                elif module not in ALLOWED_STDLIB_MODULES and module not in ALLOWED_THIRDPARTY_MODULES:
                    failed.append(f"import of {module!r} is not on the allowlist")
        elif isinstance(node, ast.ImportFrom):
            import_count += 1
            module = node.module or ""
            if node.level and node.level > 0:
                failed.append("relative imports are not permitted in candidate strategies")
                continue
            if _root_module(module) == "src":
                if module not in ALLOWED_PLATFORM_MODULES:
                    failed.append(f"import from {module!r} is not on the platform module allowlist")
            elif module not in ALLOWED_STDLIB_MODULES and module not in ALLOWED_THIRDPARTY_MODULES:
                failed.append(f"import from {module!r} is not on the allowlist")
    if import_count > MAX_IMPORTS:
        failed.append(f"too many imports ({import_count} > {MAX_IMPORTS})")
    return failed


def _check_forbidden_calls(tree: ast.AST) -> list[str]:
    failed: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALL_NAMES and node.func.id not in seen:
                seen.add(node.func.id)
                failed.append(f"call to forbidden builtin {node.func.id!r} is not permitted")
    return failed


def _check_forbidden_attributes(tree: ast.AST) -> list[str]:
    failed: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTE_NAMES:
            if node.attr not in seen:
                seen.add(node.attr)
                failed.append(f"access to forbidden attribute {node.attr!r} is not permitted")
    return failed


def _check_forbidden_names(tree: ast.AST) -> list[str]:
    failed: list[str] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.alias):
            name = (node.asname or node.name).split(".")[-1]
        elif isinstance(node, ast.Attribute):
            name = node.attr
        if name in FORBIDDEN_NAMES:
            failed.append(
                f"reference to {name!r} is not permitted -- a candidate must never "
                f"register itself as a production strategy"
            )
    return sorted(set(failed))


def _check_class_shape(tree: ast.AST) -> tuple[list[str], list[str]]:
    failed: list[str] = []
    warnings: list[str] = []
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]

    if not classes:
        return (["candidate source must define exactly one class"], warnings)
    if len(classes) > MAX_CLASSES:
        failed.append(f"candidate source defines {len(classes)} classes; exactly {MAX_CLASSES} is required")

    cls = classes[0]
    base_names = [
        base.id if isinstance(base, ast.Name) else getattr(base, "attr", "<unknown>")
        for base in cls.bases
    ]
    if REQUIRED_BASE_NAME not in base_names:
        failed.append(
            f"candidate class {cls.name!r} must subclass {REQUIRED_BASE_NAME!r} "
            f"(from src.strategies.sdk) -- got bases {base_names}"
        )
    if len(cls.bases) > 1:
        failed.append(
            f"candidate class {cls.name!r} must not use multiple inheritance "
            f"(got bases {base_names})"
        )

    methods = {
        node.name
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = REQUIRED_METHODS - methods
    if missing:
        failed.append(f"candidate class {cls.name!r} is missing required method(s): {sorted(missing)}")

    if len(methods) > MAX_FUNCTIONS_PER_CLASS:
        failed.append(
            f"candidate class {cls.name!r} defines {len(methods)} methods "
            f"(> {MAX_FUNCTIONS_PER_CLASS}) -- keep candidates small and reviewable"
        )

    if not ast.get_docstring(cls):
        warnings.append(f"candidate class {cls.name!r} has no docstring")

    return failed, warnings
