from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class DryOrderPlan:
    symbol: str
    category: str
    margin_coin: str
    margin_mode: str
    qty: str
    long_open_payload: dict
    long_close_payload: dict
    short_open_payload: dict
    short_close_payload: dict


def _client_oid(prefix: str, symbol: str) -> str:
    # Bitget clientOid must be <=32 chars and match allowed characters.
    ts = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
    clean_symbol = symbol.lower().replace("/", "_").replace(":", "_")
    return f"{prefix}_{clean_symbol}_{ts}"[:32]


def market_payload(
    *,
    symbol: str,
    category: str,
    margin_coin: str,
    margin_mode: str,
    side: str,
    pos_side: str,
    qty: str,
    reduce_only: bool = False,
    client_oid_prefix: str = "isp",
) -> dict:
    payload = {
        "symbol": symbol,
        "category": category,
        "marginCoin": margin_coin,
        "marginMode": margin_mode,
        "orderType": "market",
        "side": side,
        "posSide": pos_side,
        "qty": str(qty),
        "clientOid": _client_oid(client_oid_prefix, symbol),
    }
    if reduce_only:
        payload["reduceOnly"] = "yes"
    return payload


def build_dry_order_plan(
    *,
    symbol: str,
    category: str,
    margin_coin: str,
    margin_mode: str,
    qty: str,
) -> DryOrderPlan:
    return DryOrderPlan(
        symbol=symbol,
        category=category,
        margin_coin=margin_coin,
        margin_mode=margin_mode,
        qty=str(qty),
        long_open_payload=market_payload(
            symbol=symbol, category=category, margin_coin=margin_coin, margin_mode=margin_mode,
            side="buy", pos_side="long", qty=qty, client_oid_prefix="drylo",
        ),
        long_close_payload=market_payload(
            symbol=symbol, category=category, margin_coin=margin_coin, margin_mode=margin_mode,
            side="sell", pos_side="long", qty=qty, reduce_only=True, client_oid_prefix="drylc",
        ),
        short_open_payload=market_payload(
            symbol=symbol, category=category, margin_coin=margin_coin, margin_mode=margin_mode,
            side="sell", pos_side="short", qty=qty, client_oid_prefix="dryso",
        ),
        short_close_payload=market_payload(
            symbol=symbol, category=category, margin_coin=margin_coin, margin_mode=margin_mode,
            side="buy", pos_side="short", qty=qty, reduce_only=True, client_oid_prefix="drysc",
        ),
    )
