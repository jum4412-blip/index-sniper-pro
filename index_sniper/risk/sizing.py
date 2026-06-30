from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, getcontext
from typing import Any

getcontext().prec = 28


@dataclass(frozen=True)
class SizePlan:
    equity: float
    available: float
    capital_total: float
    capital_per_symbol: float
    leverage: int
    notional_per_symbol: float
    price: float
    raw_qty: str
    final_qty: str
    min_order_qty: str
    min_order_amount: str
    quantity_precision: int
    quantity_multiplier: str
    valid: bool
    reason: str


def _d(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def extract_usdt_equity_available(assets_response: dict[str, Any]) -> tuple[float, float]:
    data = assets_response.get("data")
    if isinstance(data, dict):
        for row in data.get("assets", []) if isinstance(data.get("assets"), list) else []:
            if isinstance(row, dict) and str(row.get("coin", "")).upper() == "USDT":
                equity = float(row.get("equity") or row.get("balance") or 0)
                available = float(row.get("available") or equity or 0)
                return equity, available
        equity = float(data.get("usdtEquity") or data.get("effEquity") or data.get("accountEquity") or 0)
        return equity, equity
    raise RuntimeError(f"Unable to parse assets response: {assets_response}")


def extract_instrument(instruments_response: dict[str, Any], symbol: str) -> dict[str, Any]:
    rows = instruments_response.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("list") or [rows]
    for row in rows:
        if isinstance(row, dict) and row.get("symbol") == symbol:
            return row
    if rows and isinstance(rows[0], dict):
        return rows[0]
    raise RuntimeError(f"instrument not found for {symbol}: {instruments_response}")


def extract_symbol_config(settings_response: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    data = settings_response.get("data") or {}
    rows = data.get("symbolConfigList") or []
    for row in rows:
        if isinstance(row, dict) and row.get("symbol") == symbol:
            return row
    return None


def round_down_to_step(value: Decimal, step: Decimal, precision: int) -> Decimal:
    if step > 0:
        units = (value / step).to_integral_value(rounding=ROUND_DOWN)
        value = units * step
    quant = Decimal("1") if precision <= 0 else Decimal("1") / (Decimal(10) ** precision)
    return value.quantize(quant, rounding=ROUND_DOWN)


def build_size_plan(*, equity: float, available: float, symbol_count: int, capital_ratio: float, leverage: int, price: float, instrument: dict[str, Any]) -> SizePlan:
    if symbol_count <= 0:
        raise ValueError("symbol_count must be positive")
    equity_d = _d(equity)
    available_d = _d(available)
    price_d = _d(price)
    capital_total = available_d * _d(capital_ratio)
    capital_per_symbol = capital_total / _d(symbol_count)
    notional = capital_per_symbol * _d(leverage)
    raw_qty = Decimal("0") if price_d <= 0 else notional / price_d

    quantity_precision = int(instrument.get("quantityPrecision") or 0)
    quantity_multiplier = _d(instrument.get("quantityMultiplier"), "0")
    min_order_qty = _d(instrument.get("minOrderQty"), "0")
    min_order_amount = _d(instrument.get("minOrderAmount"), "0")
    max_market_qty = _d(instrument.get("maxMarketOrderQty"), "0")

    final_qty_d = round_down_to_step(raw_qty, quantity_multiplier, quantity_precision)
    reason = "ok"
    valid = True
    if price_d <= 0:
        valid, reason = False, "price <= 0"
    elif final_qty_d <= 0:
        valid, reason = False, "calculated qty <= 0"
    elif min_order_qty > 0 and final_qty_d < min_order_qty:
        valid, reason = False, f"qty below minOrderQty {min_order_qty}"
    elif min_order_amount > 0 and final_qty_d * price_d < min_order_amount:
        valid, reason = False, f"notional below minOrderAmount {min_order_amount}"
    elif max_market_qty > 0 and final_qty_d > max_market_qty:
        final_qty_d = round_down_to_step(max_market_qty, quantity_multiplier, quantity_precision)
        reason = f"qty capped to maxMarketOrderQty {max_market_qty}"

    return SizePlan(
        equity=float(equity_d),
        available=float(available_d),
        capital_total=float(capital_total),
        capital_per_symbol=float(capital_per_symbol),
        leverage=leverage,
        notional_per_symbol=float(notional),
        price=float(price_d),
        raw_qty=str(raw_qty),
        final_qty=str(final_qty_d),
        min_order_qty=str(min_order_qty),
        min_order_amount=str(min_order_amount),
        quantity_precision=quantity_precision,
        quantity_multiplier=str(quantity_multiplier),
        valid=valid,
        reason=reason,
    )
