from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def celsius_to_fahrenheit(value_c: Decimal | float | str) -> Decimal:
    """Convert the source-precision Celsius value without intermediate rounding."""
    value = value_c if isinstance(value_c, Decimal) else Decimal(str(value_c))
    return value * Decimal(9) / Decimal(5) + Decimal(32)


def fahrenheit_to_celsius(value_f: Decimal | float | str) -> Decimal:
    """Convert source-precision Fahrenheit without intermediate rounding."""
    value = value_f if isinstance(value_f, Decimal) else Decimal(str(value_f))
    return (value - Decimal(32)) * Decimal(5) / Decimal(9)


def round_whole_degree(value: Decimal | float | str) -> Decimal:
    """Apply the project's explicit final whole-degree settlement rounding."""
    numeric = value if isinstance(value, Decimal) else Decimal(str(value))
    return numeric.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
