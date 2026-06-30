from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _to_decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def position_rows(position_response: dict[str, Any]) -> list[dict[str, Any]]:
    data = position_response.get("data")
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("list", "positions", "positionList", "result", "data"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [x for x in rows if isinstance(x, dict)]
        if any(k in data for k in ("symbol", "posSide", "holdSide", "total", "available", "positionSize", "size")):
            return [data]
    return []


def row_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("instId") or "").upper()


def row_qty(row: dict[str, Any]) -> Decimal:
    for key in (
        "total", "available", "positionSize", "holdVolume", "holdVol", "volume",
        "size", "qty", "contracts", "positionQty", "openQty",
    ):
        qty = abs(_to_decimal(row.get(key)))
        if qty > 0:
            return qty
    return Decimal("0")


def open_positions(position_response: dict[str, Any], *, symbol: str | None = None) -> list[dict[str, Any]]:
    target = symbol.upper() if symbol else None
    open_rows: list[dict[str, Any]] = []
    for row in position_rows(position_response):
        if target and row_symbol(row) and row_symbol(row) != target:
            continue
        qty = row_qty(row)
        if qty > 0:
            copied = dict(row)
            copied["_parsed_qty"] = str(qty)
            open_rows.append(copied)
    return open_rows
