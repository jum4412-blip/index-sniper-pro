"""Sanitized Bitget UTA evidence snapshot; research only, GET requests only.

Run on the EC2 checkout with:
    ./.venv/bin/python -B research_update_400/read_only_live_evidence.py

The output does not include API credentials, account/order IDs, or raw API
responses. A pending stop count is not proof that an open position is covered.
"""

import sys
sys.dont_write_bytecode = True

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from basic_core.account import margin_breakdown
from basic_core.api import Rest, credentials
from basic_core.runtime import connection


SYMBOLS = ("BTCUSDT", "ETHUSDT")
CATEGORY = "USDT-FUTURES"


def decimal(value):
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError("invalid number") from None
    if not result.is_finite():
        raise ValueError("invalid number")
    return result


def fmt(value):
    return format(decimal(value), "f")


def collect(api):
    """Query only private GET-backed adapter methods; return allowlisted fields."""
    assets = api.assets()
    if not isinstance(assets, dict):
        raise ValueError("invalid account response")
    account = {key: fmt(assets[key]) for key in
               ("usdtEquity", "effEquity", "imr", "mmr", "mgnRatio")}
    if decimal(account["usdtEquity"]) <= 0 or decimal(account["effEquity"]) <= 0:
        raise ValueError("invalid account equity")
    if any(decimal(account[key]) < 0 for key in ("imr", "mmr", "mgnRatio")):
        raise ValueError("invalid margin")
    account["available_usdt"] = fmt(margin_breakdown(assets)["available_usdt"])

    positions = api.positions(CATEGORY)
    stops = api.strategies(CATEGORY, "tpsl")
    if not isinstance(positions, list) or not isinstance(stops, list):
        raise ValueError("invalid position or strategy response")
    result = {
        "status": "READ_ONLY_SNAPSHOT",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": account,
        "symbols": {},
        "limitations": "Visible pending stop counts do not establish ownership, full coverage, fills, or dynamic modification.",
    }
    for symbol in SYMBOLS:
        rates = api.get("/api/v3/account/fee-rate",
                        {"symbol": symbol, "category": CATEGORY}, private=True)
        if not isinstance(rates, dict):
            raise ValueError("invalid fee response")
        fees = {key: fmt(rates[key]) for key in ("makerFeeRate", "takerFeeRate")}
        if any(decimal(v) < 0 for v in fees.values()):
            raise ValueError("invalid fee rate")
        mine = [p for p in positions if p.get("symbol") == symbol]
        gross = sum((abs(decimal(p["total"]) * decimal(p["markPrice"]))
                     for p in mine), Decimal(0))
        protective = sum(
            1 for order in stops
            if order.get("symbol") == symbol
            and order.get("status") == "pending"
            and order.get("slTriggerBy") == "mark"
            and order.get("slOrderType") == "market"
            and order.get("stopLoss") not in (None, "")
            and decimal(order["stopLoss"]) > 0
        )
        result["symbols"][symbol] = {
            "fee_rates": fees,
            "open_position_count": len(mine),
            "gross_position_notional_usdt": format(gross, "f"),
            "visible_pending_mark_market_stop_count": protective,
        }
    return result


def main(root=ROOT):
    try:
        # Rest(write=False) rejects all POST calls even if later code changes.
        api = Rest(credentials(connection(Path(root))["env"]), write=False)
        output = collect(api)
    except Exception:
        # Never print raw exceptions: some clients include request metadata.
        output = {"status": "BLOCKED", "reason": "PRIVATE_READ_OR_SCHEMA_ERROR"}
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0 if output["status"] == "READ_ONLY_SNAPSHOT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
