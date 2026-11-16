"""Decimal helpers for all money and share arithmetic.

Floats never enter the accounting path: provider payloads that arrive as JSON numbers are
converted through ``repr`` so the exact printed digits are preserved.
"""

from __future__ import annotations

import decimal
from decimal import ROUND_DOWN, ROUND_UP, Decimal

for _ctx in (decimal.DefaultContext, decimal.getcontext()):
    _ctx.prec = 40
    _ctx.traps[decimal.FloatOperation] = True

ZERO = Decimal(0)
ONE = Decimal(1)
# USDC / pUSD and conditional tokens both use 6 on-chain decimals.
USDC_QUANTUM = Decimal("0.000001")
SHARE_QUANTUM = Decimal("0.000001")
PRICE_QUANTUM = Decimal("0.000001")


def to_decimal(value: object) -> Decimal:
    """Convert provider/user input to Decimal without binary float artifacts."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool is not a monetary value")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except decimal.InvalidOperation as exc:
            raise ValueError(f"not a decimal: {value!r}") from exc
    raise TypeError(f"unsupported monetary type: {type(value).__name__}")


def opt_decimal(value: object) -> Decimal | None:
    """Like :func:`to_decimal` but maps ``None`` to ``None`` (unavailable, never zero)."""
    return None if value is None else to_decimal(value)


def usdc_down(value: Decimal) -> Decimal:
    return value.quantize(USDC_QUANTUM, rounding=ROUND_DOWN)


def usdc_up(value: Decimal) -> Decimal:
    return value.quantize(USDC_QUANTUM, rounding=ROUND_UP)


def shares_down(value: Decimal) -> Decimal:
    return value.quantize(SHARE_QUANTUM, rounding=ROUND_DOWN)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round ``value`` down to a multiple of ``step`` (e.g. order size increments)."""
    if step <= ZERO:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))
