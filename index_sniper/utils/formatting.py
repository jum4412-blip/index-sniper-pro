from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, getcontext
from typing import Any

getcontext().prec = 28


def _d(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def format_price(value: float | Decimal, instrument: dict[str, Any]) -> str:
    price = _d(value)
    precision = int(instrument.get("pricePrecision") or 0)
    step = _d(instrument.get("priceMultiplier"), "0")
    if step > 0:
        units = (price / step).to_integral_value(rounding=ROUND_HALF_UP)
        price = units * step
    quant = Decimal("1") if precision <= 0 else Decimal("1") / (Decimal(10) ** precision)
    price = price.quantize(quant, rounding=ROUND_DOWN)
    return format(price, "f")
