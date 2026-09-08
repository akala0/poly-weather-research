"""Exact representation helpers; no source identity or queue authorization."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any


def exact_decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite trade Decimal")
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def parse_trade_timestamp(value: Any) -> datetime | None:
    """No float conversion, time tolerance, naive timezone or lost submicrosecond."""
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        text = str(value)
        fractional = re.search(r"\d{2}:\d{2}:\d{2}[.,](\d+)", text)
        if fractional and any(char != "0" for char in fractional[1][6:]):
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo else None
        except (TypeError, ValueError):
            return None
    if not number.is_finite():
        return None
    micros = Fraction(number) * (1000 if number > 10**11 else 1_000_000)
    if micros.denominator != 1:
        return None
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=micros.numerator)
    except (OverflowError, ValueError):
        return None
