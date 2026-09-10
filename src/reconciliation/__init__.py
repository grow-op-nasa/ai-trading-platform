"""Fill reconciliation -- Sprint 5.

Compares `PaperBroker`'s simulated fills (`src/execution`, ADR-0022 --
instant, complete, at a caller-supplied price) against what a real
broker order actually did (`src/broker`, ADR-0023/ADR-0024). This is a
deliberate exception to ADR-0024's rule keeping `src/broker` and
`src/execution` independent of each other: that rule exists to stop
`src/broker` from *depending on* `src/execution`'s models so the
dependency direction stays honest; reconciliation is a separate,
higher-level analysis layer that exists specifically to compare the two
worlds, the same way `src/attribution` depends on both `src/backtesting`
and `src/regime` without either of those depending on it back. See
`DECISIONS.md`, ADR-0027.

Scope this round is a single-order comparison primitive
(`reconcile_fill`) -- aggregating reconciliations across many trades
into a summary report is a natural future step, not built here.
"""

from src.reconciliation.engine import reconcile_fill
from src.reconciliation.models import FillReconciliation

__all__ = [
    "reconcile_fill",
    "FillReconciliation",
]
