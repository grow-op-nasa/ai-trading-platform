"""Tests for the static safety validator -- src/ai/agents/strategy_dev/safety.py
(Sprint 14 spec, sections 9-20, 30, 95, Phase 11). The single most
safety-critical module in the whole package: every case here is a
representative bypass attempt an AI-generated candidate could plausibly
produce, not just an obvious "imports os" case. `validate_candidate_source()`
must never raise on malformed/malicious input -- every case is asserted
via its returned `ValidationResult`, never a caught exception. No heavy
dependencies (`safety.py` imports only `ast` and stdlib dataclasses).
"""

from __future__ import annotations

from src.ai.agents.strategy_dev.safety import (
    ALLOWED_PLATFORM_MODULES,
    ALLOWED_STDLIB_MODULES,
    ALLOWED_THIRDPARTY_MODULES,
    MAX_FUNCTIONS_PER_CLASS,
    MAX_IMPORTS,
    MAX_SOURCE_CHARS,
    validate_candidate_source,
)

VALID_SOURCE = '''\
"""A well-formed candidate."""
from src.strategies.sdk import BaseStrategy
from src.signals.models import SignalDirection


class EmaVolFilterCandidate(BaseStrategy):
    """EMA cross gated by volatility."""

    def __init__(self, symbol: str, fast: int = 10, slow: int = 30):
        super().__init__(name="ema_vol_filter", symbol=symbol)
        self._fast = fast
        self._slow = slow

    def prepare(self, data):
        self.require_columns(data, "close")
        out = data.copy()
        out["ema_fast"] = self.indicator(data, "EMA", period=self._fast)
        out["ema_slow"] = self.indicator(data, "EMA", period=self._slow)
        return out

    def generate_signals(self, data):
        signals = []
        in_position = False
        for timestamp, row in data.iterrows():
            crossed_up = row["ema_fast"] > row["ema_slow"]
            if crossed_up and not in_position:
                signals.append(self.emit_signal(timestamp, SignalDirection.LONG, confidence=0.6))
                in_position = True
            elif not crossed_up and in_position:
                signals.append(self.emit_signal(timestamp, SignalDirection.FLAT, confidence=0.6))
                in_position = False
        return signals
'''


def test_valid_candidate_passes_with_no_failures():
    result = validate_candidate_source(VALID_SOURCE)
    assert result.passed is True
    assert result.failed_checks == ()


# ---------------------------------------------------------------------------
# Empty / malformed input -- must never raise.
# ---------------------------------------------------------------------------


def test_empty_source_fails_cleanly():
    result = validate_candidate_source("")
    assert result.passed is False
    assert "empty" in result.failed_checks[0]


def test_whitespace_only_source_fails_cleanly():
    result = validate_candidate_source("   \n\t  \n")
    assert result.passed is False


def test_syntax_error_fails_cleanly_never_raises():
    result = validate_candidate_source("class Broken(:\n    def prepare(self")
    assert result.passed is False
    assert "syntax error" in result.failed_checks[0]


def test_oversized_source_is_rejected():
    padding = "\n# padding" * (MAX_SOURCE_CHARS // 5)
    result = validate_candidate_source(VALID_SOURCE + padding)
    assert result.passed is False
    assert any("maximum size" in c for c in result.failed_checks)


# ---------------------------------------------------------------------------
# Import allowlist -- default-deny, exact-match platform paths only.
# ---------------------------------------------------------------------------


def _swap_import(extra_import: str, body: str = "        return data\n") -> str:
    return f"""\
{extra_import}
from src.strategies.sdk import BaseStrategy


class Evil(BaseStrategy):
    def prepare(self, data):
{body}
    def generate_signals(self, data):
        return []
"""


def test_rejects_os_import():
    result = validate_candidate_source(_swap_import("import os"))
    assert result.passed is False
    assert any("os" in c for c in result.failed_checks)


def test_rejects_subprocess_import():
    result = validate_candidate_source(_swap_import("import subprocess"))
    assert result.passed is False


def test_rejects_socket_import():
    result = validate_candidate_source(_swap_import("import socket"))
    assert result.passed is False


def test_rejects_requests_import():
    result = validate_candidate_source(_swap_import("import requests"))
    assert result.passed is False


def test_rejects_urllib_import():
    result = validate_candidate_source(_swap_import("import urllib.request"))
    assert result.passed is False


def test_rejects_pickle_import():
    result = validate_candidate_source(_swap_import("import pickle"))
    assert result.passed is False


def test_rejects_ctypes_import():
    result = validate_candidate_source(_swap_import("import ctypes"))
    assert result.passed is False


def test_rejects_multiprocessing_and_threading_imports():
    for module in ("multiprocessing", "threading"):
        result = validate_candidate_source(_swap_import(f"import {module}"))
        assert result.passed is False, module


def test_rejects_importlib_import():
    result = validate_candidate_source(_swap_import("import importlib"))
    assert result.passed is False


def test_rejects_inspect_import():
    result = validate_candidate_source(_swap_import("import inspect"))
    assert result.passed is False


def test_rejects_pathlib_and_shutil_imports():
    for module in ("pathlib", "shutil"):
        result = validate_candidate_source(_swap_import(f"import {module}"))
        assert result.passed is False, module


def test_rejects_non_exact_platform_module_path():
    # A prefix match against an allowed platform package must not slip
    # through -- only the exact dotted paths in ALLOWED_PLATFORM_MODULES
    # are permitted.
    result = validate_candidate_source(_swap_import("import src.strategies.registry"))
    assert result.passed is False


def test_rejects_relative_import():
    source = """\
from . import something
from src.strategies.sdk import BaseStrategy


class Evil(BaseStrategy):
    def prepare(self, data):
        return data

    def generate_signals(self, data):
        return []
"""
    result = validate_candidate_source(source)
    assert result.passed is False
    assert any("relative import" in c for c in result.failed_checks)


def test_rejects_forbidden_platform_package_even_via_from_import():
    result = validate_candidate_source(_swap_import("from src.execution.engine import PaperBroker"))
    assert result.passed is False


def test_allows_every_declared_allowlist_module():
    # Positive half: every module actually on the allowlist must import
    # cleanly (checked via the import-only path, not full class-shape
    # validity, since not every allowed module is meant to be
    # instantiated standalone in a fixture like this).
    for module in sorted(ALLOWED_STDLIB_MODULES | ALLOWED_THIRDPARTY_MODULES):
        source = _swap_import(f"import {module}")
        result = validate_candidate_source(source)
        # Only the import itself should be judged here -- assert the
        # specific "not on the allowlist" failure never appears for an
        # allowed module.
        assert not any("not on the allowlist" in c for c in result.failed_checks), module


def test_too_many_imports_is_rejected():
    imports = "\n".join(f"import math" for _ in range(MAX_IMPORTS + 5))
    source = f"""\
{imports}
from src.strategies.sdk import BaseStrategy


class Evil(BaseStrategy):
    def prepare(self, data):
        return data

    def generate_signals(self, data):
        return []
"""
    result = validate_candidate_source(source)
    assert result.passed is False
    assert any("too many imports" in c for c in result.failed_checks)


# ---------------------------------------------------------------------------
# Dynamic execution denial.
# ---------------------------------------------------------------------------


def _with_call(call_expr: str) -> str:
    return f"""\
from src.strategies.sdk import BaseStrategy


class Evil(BaseStrategy):
    def prepare(self, data):
        {call_expr}
        return data

    def generate_signals(self, data):
        return []
"""


def test_rejects_eval_call():
    result = validate_candidate_source(_with_call('eval("1+1")'))
    assert result.passed is False
    assert any("eval" in c for c in result.failed_checks)


def test_rejects_exec_call():
    result = validate_candidate_source(_with_call('exec("x = 1")'))
    assert result.passed is False


def test_rejects_compile_call():
    result = validate_candidate_source(_with_call('compile("1+1", "<s>", "eval")'))
    assert result.passed is False


def test_rejects_dunder_import_call():
    result = validate_candidate_source(_with_call('__import__("os")'))
    assert result.passed is False


def test_rejects_getattr_setattr_delattr():
    for call in ('getattr(self, "x")', 'setattr(self, "x", 1)', 'delattr(self, "x")'):
        result = validate_candidate_source(_with_call(call))
        assert result.passed is False, call


def test_rejects_globals_locals_vars_calls():
    for call in ("globals()", "locals()", "vars()"):
        result = validate_candidate_source(_with_call(call))
        assert result.passed is False, call


def test_rejects_open_call():
    result = validate_candidate_source(_with_call('open("/etc/passwd")'))
    assert result.passed is False


def test_rejects_input_and_breakpoint_calls():
    for call in ("input()", "breakpoint()"):
        result = validate_candidate_source(_with_call(call))
        assert result.passed is False, call


# ---------------------------------------------------------------------------
# Sandbox-escape gadget attributes -- not just "obvious" attempts.
# ---------------------------------------------------------------------------


def _with_attr_access(expr: str) -> str:
    return f"""\
from src.strategies.sdk import BaseStrategy


class Evil(BaseStrategy):
    def prepare(self, data):
        gadget = {expr}
        return data

    def generate_signals(self, data):
        return []
"""


def test_rejects_subclasses_gadget():
    result = validate_candidate_source(_with_attr_access('object().__class__.__subclasses__()'))
    assert result.passed is False
    assert any("__subclasses__" in c or "__class__" in c for c in result.failed_checks)


def test_rejects_bases_gadget():
    result = validate_candidate_source(_with_attr_access("self.__class__.__bases__"))
    assert result.passed is False


def test_rejects_mro_gadget():
    result = validate_candidate_source(_with_attr_access("type(self).__mro__"))
    assert result.passed is False


def test_rejects_globals_dunder_on_a_function():
    result = validate_candidate_source(_with_attr_access("self.prepare.__globals__"))
    assert result.passed is False


def test_rejects_builtins_dunder():
    result = validate_candidate_source(_with_attr_access("self.__builtins__"))
    assert result.passed is False


def test_rejects_code_and_closure_dunders():
    for attr in ("__code__", "__closure__"):
        result = validate_candidate_source(_with_attr_access(f"self.prepare.{attr}"))
        assert result.passed is False, attr


def test_rejects_reduce_gadgets_used_for_pickle_style_escapes():
    for attr in ("__reduce__", "__reduce_ex__"):
        result = validate_candidate_source(_with_attr_access(f"self.{attr}"))
        assert result.passed is False, attr


def test_rejects_dict_dunder_access():
    result = validate_candidate_source(_with_attr_access("self.__dict__"))
    assert result.passed is False


# ---------------------------------------------------------------------------
# Self-registration -- a candidate must never register itself as a
# production strategy.
# ---------------------------------------------------------------------------


def test_rejects_register_strategy_reference():
    source = """\
from src.strategies.sdk import BaseStrategy
from src.strategies.registry import register_strategy


class Evil(BaseStrategy):
    def prepare(self, data):
        return data

    def generate_signals(self, data):
        return []
"""
    result = validate_candidate_source(source)
    assert result.passed is False
    assert any("register_strategy" in c for c in result.failed_checks)
    # Also caught for the disallowed platform import itself.
    assert any("not on the platform module allowlist" in c for c in result.failed_checks)


def test_rejects_register_strategy_used_as_a_decorator_name_only():
    # Even referencing the bare name (no import at all -- e.g. assuming
    # it's globally available, or referencing it as a string-adjacent
    # identifier) must be caught by the forbidden-names scan, not only
    # the forbidden-import scan.
    source = """\
from src.strategies.sdk import BaseStrategy


class Evil(BaseStrategy):
    def prepare(self, data):
        register_strategy(self)
        return data

    def generate_signals(self, data):
        return []
"""
    result = validate_candidate_source(source)
    assert result.passed is False
    assert any("register_strategy" in c for c in result.failed_checks)


# ---------------------------------------------------------------------------
# Class shape -- exactly one class, correct base, required methods.
# ---------------------------------------------------------------------------


def test_rejects_source_with_no_class_at_all():
    source = """\
def prepare(data):
    return data
"""
    result = validate_candidate_source(source)
    assert result.passed is False
    assert any("exactly one class" in c for c in result.failed_checks)


def test_rejects_multiple_classes():
    source = """\
from src.strategies.sdk import BaseStrategy


class Helper:
    pass


class Evil(BaseStrategy):
    def prepare(self, data):
        return data

    def generate_signals(self, data):
        return []
"""
    result = validate_candidate_source(source)
    assert result.passed is False
    assert any("defines 2 classes" in c for c in result.failed_checks)


def test_rejects_missing_base_strategy_base_class():
    source = """\
class Evil:
    def prepare(self, data):
        return data

    def generate_signals(self, data):
        return []
"""
    result = validate_candidate_source(source)
    assert result.passed is False
    assert any("must subclass" in c for c in result.failed_checks)


def test_rejects_multiple_inheritance():
    # Exactly one class (so MAX_CLASSES=1 doesn't also fire and mask the
    # specific check under test -- _check_class_shape() inspects
    # `classes[0]` only), with two bases on that single class.
    source = """\
from src.strategies.sdk import BaseStrategy


class Evil(BaseStrategy, dict):
    def prepare(self, data):
        return data

    def generate_signals(self, data):
        return []
"""
    result = validate_candidate_source(source)
    assert result.passed is False
    assert any("multiple inheritance" in c for c in result.failed_checks)


def test_rejects_missing_required_methods():
    source = """\
from src.strategies.sdk import BaseStrategy


class Evil(BaseStrategy):
    def prepare(self, data):
        return data
"""
    result = validate_candidate_source(source)
    assert result.passed is False
    assert any("missing required method" in c for c in result.failed_checks)


def test_rejects_too_many_methods_on_the_class():
    methods = "\n".join(
        f"    def helper_{i}(self):\n        return {i}\n" for i in range(MAX_FUNCTIONS_PER_CLASS + 5)
    )
    source = f"""\
from src.strategies.sdk import BaseStrategy


class Evil(BaseStrategy):
{methods}
    def prepare(self, data):
        return data

    def generate_signals(self, data):
        return []
"""
    result = validate_candidate_source(source)
    assert result.passed is False
    assert any("methods" in c and "keep candidates small" in c for c in result.failed_checks)


def test_missing_docstring_is_a_warning_not_a_failure():
    source = """\
from src.strategies.sdk import BaseStrategy


class Evil(BaseStrategy):
    def prepare(self, data):
        return data

    def generate_signals(self, data):
        return []
"""
    result = validate_candidate_source(source)
    assert result.passed is True  # no docstring is not a hard failure
    assert any("docstring" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Combined/compound bypass attempts -- multiple failures at once must
# all be reported, never just the first one found.
# ---------------------------------------------------------------------------


def test_multiple_simultaneous_violations_are_all_reported():
    source = """\
import os
import subprocess


class Evil:
    def prepare(self, data):
        eval("1+1")
        return os.environ

    def generate_signals(self, data):
        register_strategy(self)
        return []
"""
    result = validate_candidate_source(source)
    assert result.passed is False
    # At minimum: forbidden imports (os, subprocess), forbidden eval
    # call, missing BaseStrategy base, and register_strategy reference.
    assert len(result.failed_checks) >= 4
