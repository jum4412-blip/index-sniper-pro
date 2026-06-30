from __future__ import annotations

from typing import Any


def extract_usdt_equity(assets_response: dict[str, Any]) -> float | None:
    data = assets_response.get("data")
    if data is None:
        return None

    candidates = []
    if isinstance(data, dict):
        if isinstance(data.get("assets"), list):
            candidates = data.get("assets") or []
        elif isinstance(data.get("list"), list):
            candidates = data.get("list") or []
        else:
            candidates = [data]
    elif isinstance(data, list):
        candidates = data

    for item in candidates:
        if not isinstance(item, dict):
            continue
        coin = item.get("coin") or item.get("marginCoin") or item.get("assetCoin")
        if coin == "USDT":
            for key in ("equity", "available", "availableBalance", "balance", "usdtEquity"):
                value = item.get(key)
                try:
                    if value is not None:
                        return float(value)
                except (TypeError, ValueError):
                    continue
    return None


def first_instrument(instruments_response: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    data = instruments_response.get("data") or []
    if isinstance(data, dict):
        data = [data]
    for item in data:
        if isinstance(item, dict) and item.get("symbol") == symbol:
            return item
    return data[0] if data and isinstance(data[0], dict) else None
