"""Per-signal risk auditability -- Sprint 11 (`DECISIONS.md`, ADR-0044).

A strategy signal that Risk correctly refuses is not "no trade" -- it is
research evidence in its own right (Sprint 11 spec, sections 20-21,
48). This is especially important for an ML strategy
(`src.strategies.ai_signal.AISignalStrategy`): a model that generates
plausible-looking signals the risk layer correctly declines to trade
(insufficient risk budget, an exposure limit already at capacity, an
unavailable stop) tells a researcher something real about how the
model's confidence and the portfolio's actual capacity interact --
information a `BacktestResult` that only ever showed "signals" and
"trades" would silently discard.

`SignalOutcome` records, for every signal `PortfolioBacktestEngine`
ever considered, exactly what happened to it -- approved and sized by
`PortfolioRiskEngine`, rejected by it, or never reaching it at all
because a stop couldn't be computed. `summarize_outcomes()` rolls a
list of these into the small aggregate view Sprint 11 spec section 21
asks for (total signals / approved / rejected / rejection counts by
reason) without ever reducing an individual rejection to a bare "no
trade" string.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from src.risk.models import RiskDecision
from src.signals.models import Signal

STOP_UNAVAILABLE = "STOP_UNAVAILABLE"


@dataclass(frozen=True)
class SignalOutcome:
    """What became of one signal `PortfolioBacktestEngine` processed.

    Args:
        signal: the originating signal -- symbol, direction, timestamp,
            confidence, and whatever metadata its strategy attached
            (e.g. an `AISignalStrategy`'s `model_id`/`class_probability`)
            all travel with it, so a rejected signal remains fully
            traceable back to *why* the strategy proposed it, not just
            why Risk declined it.
        accepted: whether this signal resulted in an approved entry or
            exit. `False` covers both an outright risk rejection and a
            stop that could never be computed.
        risk_decision: the full `PortfolioRiskEngine.decide()` /
            `decide_close()` outcome, when the signal reached the risk
            engine at all. `None` only when `stop_unavailable_reason`
            is set -- a stop that could not be computed means Risk was
            never consulted in the first place (Sprint 11 spec, section
            9: an unavailable stop must not be papered over with an
            invented one just to get a `RiskDecision` to point to).
        stop_unavailable_reason: set only when this entry signal's
            `StopPolicy.stop_price()` call returned
            `StopResult.available=False` -- the human-readable reason
            from that result. Always `None` for a `FLAT`/close signal
            (a close never computes a stop) and for any entry signal
            that did reach the risk engine.

    Raises:
        ValueError: both `risk_decision` and `stop_unavailable_reason`
            are set (a signal either reached the risk engine or it
            didn't -- never both), or `accepted=True` while
            `risk_decision` is missing or itself unapproved (an
            accepted outcome must be backed by a real approval).
    """

    signal: Signal
    accepted: bool
    risk_decision: RiskDecision | None
    stop_unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.risk_decision is not None and self.stop_unavailable_reason is not None:
            raise ValueError(
                "a SignalOutcome cannot carry both a risk_decision and a "
                "stop_unavailable_reason -- a signal either reached the risk "
                "engine or it didn't"
            )
        if self.accepted and (self.risk_decision is None or not self.risk_decision.approved):
            raise ValueError(
                "accepted=True requires a risk_decision with approved=True -- "
                "an outcome can never claim acceptance without a real approval"
            )

    @property
    def rejection_reason(self) -> str | None:
        """A short, stable string identifying why this signal did not
        result in a trade -- `None` when `accepted` is `True`.

        `STOP_UNAVAILABLE` when the stop policy itself couldn't produce
        a boundary; otherwise `risk_decision.rejection_reason.value`
        (an `src.risk.models.RejectionReason` member) -- never a bare
        "no trade" string that would discard which specific constraint
        was responsible (Sprint 11 spec, section 21).
        """
        if self.accepted:
            return None
        if self.stop_unavailable_reason is not None:
            return STOP_UNAVAILABLE
        if self.risk_decision is not None and self.risk_decision.rejection_reason is not None:
            return self.risk_decision.rejection_reason.value
        return None


@dataclass(frozen=True)
class RiskAuditSummary:
    """The aggregate view Sprint 11 spec section 21 asks for: how many
    signals were seen, how many became trades, and why the rest didn't
    -- rolled up from a `BacktestResult.signal_outcomes` list, never a
    replacement for inspecting the individual `SignalOutcome`s directly.
    """

    total_signals: int
    approved_entries: int
    rejected_entries: int
    rejection_counts: dict[str, int]


def summarize_outcomes(outcomes: list[SignalOutcome]) -> RiskAuditSummary:
    """Roll a list of `SignalOutcome`s into one `RiskAuditSummary`."""
    approved = sum(1 for o in outcomes if o.accepted)
    rejected_reasons = [o.rejection_reason for o in outcomes if not o.accepted]
    counts = Counter(reason for reason in rejected_reasons if reason is not None)
    return RiskAuditSummary(
        total_signals=len(outcomes),
        approved_entries=approved,
        rejected_entries=len(outcomes) - approved,
        rejection_counts=dict(counts),
    )
