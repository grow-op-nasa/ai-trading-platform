"""Shared instrument catalogue for the paper-trading UI (Sprint 21).

This is a SUGGESTION list, not a support guarantee: the only provider today is yfinance (daily bars) and nothing here proves
that a symbol has data, a usable history or a compatible strategy. Support is established only by the authoritative preflight
(`market_data`, strategy and instrument checks) at the next explicit Run / Preflight. Adding an instrument means adding one
`Instrument` row; no page needs to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CUSTOM = "Custom…"
SUPPORTED_TIMEFRAMES = ("1d",)            # mirrors the paper runtime (closed daily bars only)

# Ordinary US tickers plus the yfinance conventions for share classes (BRK-B, BF.B), indices (^GSPC) and FX (EURUSD=X).
_SYMBOL = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=]{0,14}$")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    label: str
    category: str = "US equity"
    timeframes: tuple[str, ...] = SUPPORTED_TIMEFRAMES
    note: str = ""


CATALOGUE: tuple[Instrument, ...] = (
    Instrument("QQQ", "Invesco QQQ (Nasdaq-100 ETF)", "ETF"),
    Instrument("SPY", "SPDR S&P 500 ETF", "ETF"),
    Instrument("IWM", "iShares Russell 2000 ETF", "ETF"),
    Instrument("DIA", "SPDR Dow Jones Industrial Average ETF", "ETF"),
    Instrument("AAPL", "Apple Inc.", "US equity"),
    Instrument("MSFT", "Microsoft Corp.", "US equity"),
    Instrument("NVDA", "NVIDIA Corp.", "US equity"),
    Instrument("AMD", "Advanced Micro Devices", "US equity"),
)
_BY_SYMBOL = {i.symbol: i for i in CATALOGUE}


def symbols() -> list[str]:
    return [i.symbol for i in CATALOGUE]


def options() -> list[str]:
    """Selector options: every preset, then the Custom… entry last."""
    return [*symbols(), CUSTOM]


def lookup(symbol: str | None) -> Instrument | None:
    return _BY_SYMBOL.get(symbol or "")


def display(option: str) -> str:
    inst = lookup(option)
    return option if inst is None else f"{inst.symbol} — {inst.label}"


def normalize_symbol(text: str | None) -> tuple[str | None, str | None]:
    """(symbol, None) or (None, reason). Syntax only: trims, upper-cases, rejects empty/malformed input. It never substitutes a
    default and never claims the symbol exists -- that is the provider/preflight's job."""
    raw = (text or "").strip()
    if not raw:
        return None, "Enter a ticker symbol (for example TSLA)."
    sym = raw.upper()
    if not _SYMBOL.match(sym):
        return None, (f"{raw!r} is not a valid ticker format. Use letters and digits, optionally with '.', '-', '=' or a leading '^' "
                      "(up to 15 characters, no spaces).")
    return sym, None
