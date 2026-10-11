"""Human-operated strategy promotion -- src/promotion/ (ADR-0049).

The only code that turns a human-approved candidate into a genuinely
registered production strategy. Imported solely by the human-only
`src.cli.strategy_promote`; nothing under `src.ai.agents` may import it
(`tests/test_architecture.py`).
"""

from src.promotion.service import (
    PreflightReport,
    PromotionError,
    PromotionResult,
    PromotionService,
    contract_candles,
)

__all__ = [
    "PreflightReport",
    "PromotionError",
    "PromotionResult",
    "PromotionService",
    "contract_candles",
]
