"""Strategy identity -- src/strategies/identity.py.

Established in Sprint 6 (`DECISIONS.md`, ADR-0035) as the seam for
"which version of a strategy produced this experiment." Deliberately
not Git commit hashing and not package versioning -- both explicitly
out of scope this round -- just a hash of the strategy class's own
Python source. That's the smallest thing that actually closes the gap
described in ADR-0035: change `EMACrossStrategy`'s logic six months
from now, and any experiment recorded against the old code
automatically carries a different `strategy_version` than one recorded
against the new code, with zero effort -- and zero chance to forget --
from the strategy author.

This is a **source-identity** hash, not a **semantic-identity** hash:
two behaviorally identical strategies that differ only in a comment or
a docstring get different versions. That asymmetry is an accepted,
documented trade-off (see ADR-0035's Decision section for the
alternatives considered), not a bug to fix later.
"""

from __future__ import annotations

import inspect

from src.utils.hashing import sha256_hex


def strategy_version(strategy_cls: type) -> str:
    """SHA-256 hex digest of `strategy_cls`'s own Python source.

    Args:
        strategy_cls: a strategy class (not an instance) -- typically
            `type(some_strategy_instance)`.

    Raises:
        TypeError: `strategy_cls`'s source isn't retrievable (e.g. a
            class defined interactively rather than in a real module
            file). Fails loudly rather than silently returning a
            placeholder that would look like a real version.
    """
    try:
        source = inspect.getsource(strategy_cls)
    except (OSError, TypeError) as exc:
        raise TypeError(
            f"cannot compute a source-based version for {strategy_cls!r} -- "
            "its source code isn't retrievable (e.g. it isn't defined in a "
            "real module file). Strategy identity requires real, "
            "inspectable source."
        ) from exc
    return sha256_hex(source.encode("utf-8"))
