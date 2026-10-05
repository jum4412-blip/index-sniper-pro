"""Durable virtual exchange for forward observation; never writes to Bitget.

Quotes and contract rules come from the read-only market adapter. Executions
are adverse quote estimates, not observed fills, depth or liquidation tests.
Funding is deliberately unknown. A position spanning an observation gap is
preserved as incomplete rather than assigned an invented protective fill.
"""
from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal

from cci_chop_v1._compat.core import CAT, SYMBOLS, DataError, Rejected, SafetyError, ds, now_ms, number


STATE_KEY = "cci_chop_paper_exchange"
ACCOUNTING = "PAPER_ESTIMATE_BEFORE_FUNDING"
GAP_REASON = "PAPER_OBSERVATION_GAP_INCOMPLETE"
PUBLIC_PATHS = frozenset({
    "/api/v3/market/instruments", "/api/v3/market/candles",
    "/api/v3/market/history-candles", "/api/v3/market/tickers",
    "/api/v3/market/history-fund-rate",
})


class PaperAdapter:
    """CCAPI-compatible, single-writer simulation backed by basic_core.Store.

    ``now`` is an injectable millisecond clock. Private fees may be read once;
    an unavailable personal fee falls back explicitly to the public contract
    fee. The wrapped adapter must be read-only, even though its post method is
    never called here. No real account balances or positions are adopted.
    """

    is_paper = True
    write = False

    def __init__(self, real_api, store, config=None, initial_equity=None,
                 now=None, impact_bps=7.0, max_gap_ms=15_000):
        if getattr(real_api, "write", False):
            raise SafetyError("PAPER_REQUIRES_READ_ONLY_SOURCE")
        self.source = real_api
        self.store = store
        self.c = dict(config or {})
        self.now = now or now_ms
        self.impact_bps = number(impact_bps)
        if not 0 <= self.impact_bps <= 100:
            raise DataError("PAPER_IMPACT_INVALID")
        if isinstance(max_gap_ms, bool) or not isinstance(max_gap_ms, int) or not 1 <= max_gap_ms <= 15_000:
            raise DataError("PAPER_OBSERVATION_GAP_INVALID")
        self.max_gap_ms = max_gap_ms
        initial = number(initial_equity if initial_equity is not None else
                         self.c.get("shadow_equity_reference_usdt", 1042.19), True)
        leverage = self.c.get("leverage", 5)
        if type(leverage) is not int or not 1 <= leverage <= 5:
            raise DataError("PAPER_LEVERAGE_INVALID")
        self.leverage = leverage
        signature = hashlib.sha256(json.dumps({"initial_equity": initial, "leverage": leverage,
                            "impact_bps": self.impact_bps, "max_gap_ms": max_gap_ms},
                            sort_keys=True, allow_nan=False).encode()).hexdigest()
        existing = store.get(STATE_KEY)
        if existing is not None:
            if not isinstance(existing, dict) or existing.get("schema") != 1 or existing.get("signature") != signature:
                raise SafetyError("PAPER_EXISTING_SCENARIO_CONFIG_MISMATCH")
            self.state = existing
        else:
            self.state = {"schema": 1, "signature": signature, "created_ms": self.now(),
                "initial_equity_usdt": initial, "cash_usdt": initial, "sequence": 0,
                "valid": True, "incomplete_reason": None, "orders": {}, "clients": {},
                "fills": {}, "positions": {}, "stops": {}, "history": {}, "quotes": {},
                "fees": {}, "accounting": ACCOUNTING, "funding_accounted": False,
                "real_orders_sent": 0, "exchange_fills_verified": False,
                "maintenance_margin_verified": False, "liquidation_verified": False}
            self._save()

    def _save(self):
        self.store.set(STATE_KEY, self.state)

    def _next(self, prefix):
        self.state["sequence"] += 1
        return "PAPER_" + prefix + "_" + str(self.state["sequence"])

    def scenario_status(self):
        return {key: copy.deepcopy(self.state[key]) for key in (
            "valid", "incomplete_reason", "accounting", "funding_accounted",
            "real_orders_sent", "exchange_fills_verified", "maintenance_margin_verified",
            "liquidation_verified")}

    def instrument(self, symbol):
        if symbol not in SYMBOLS:
            raise DataError("PAPER_SYMBOL_INVALID")
        return self.source.instrument(symbol)

    def candles(self, symbol, interval, count):
        return self.source.candles(symbol, interval, count)

    def settled_funding(self, symbol, now=None):
        # Read-only funding may inform an entry veto. It is not position funding
        # accounting and never turns the paper result into exchange evidence.
        return self.source.settled_funding(symbol, now)

    def fee(self, symbol):
        if symbol not in SYMBOLS:
            raise DataError("PAPER_FEE_SYMBOL_INVALID")
        if symbol in self.state["fees"]:
            return self.state["fees"][symbol]["taker_rate"]
        source = "PUBLIC_CONTRACT_TAKER_FEE_FALLBACK"
        try:
            fee = number(self.source.fee(symbol))
            if not 0 <= fee < .01:
                raise DataError("PAPER_PERSONAL_FEE_INVALID")
            source = "PRIVATE_READ_ONLY_TAKER_FEE_OBSERVATION"
        except (SafetyError, AttributeError):
            fee = number(self.instrument(symbol).fee)
            if not 0 <= fee < .01:
                raise DataError("PAPER_PUBLIC_FEE_INVALID")
        self.state["fees"][symbol] = {"taker_rate": fee, "source": source, "read_ms": self.now()}
        self._save()
        return fee

    fee_rate = fee

    def quote(self, symbol):
        q = self.source.quote(symbol)
        q.validate(self.now())
        if q.symbol != symbol:
            raise DataError("PAPER_QUOTE_SYMBOL_MISMATCH")
        self._observe(q)
        return q

    def quotes(self, symbols=SYMBOLS):
        # Individual calls ensure each existing position's gap is evaluated.
        return {symbol: self.quote(symbol) for symbol in symbols}

    def _observe(self, q):
        at = self.now()
        p = self.state["positions"].get(q.symbol)
        if p is not None:
            elapsed = at - p["last_observed_ms"]
            if not self.state["valid"] or elapsed < 0 or elapsed > self.max_gap_ms:
                self.state["valid"] = False
                self.state["incomplete_reason"] = GAP_REASON
                p["observation_incomplete"] = True
                p["unobserved_interval_ms"] = max(0, elapsed)
                self._save()
                raise SafetyError(GAP_REASON)
            p["last_observed_ms"] = at
        self.state["quotes"][q.symbol] = {"ts": q.ts, "observed_ms": at,
            "bid": q.bid, "ask": q.ask, "mark": q.mark}
        if p is not None:
            active = [row for row in self.state["stops"].values()
                      if row["symbol"] == q.symbol and row["status"] == "pending"]
            for stop in active:
                price = number(stop["stopLoss"], True)
                if (p["posSide"] == "long" and q.mark <= price) or (p["posSide"] == "short" and q.mark >= price):
                    body = {"category": CAT, "symbol": q.symbol,
                        "side": "sell" if p["posSide"] == "long" else "buy",
                        "qty": p["total"], "orderType": "market", "reduceOnly": "yes",
                        "clientOid": self._next("STOP_CLIENT")}
                    self._place(body, q, native_stop_id=stop["orderId"])
                    break
        self._save()

    def assets(self):
        unrealized = 0.0
        margin = 0.0
        for p in self.state["positions"].values():
            quote = self.state["quotes"].get(p["symbol"])
            mark = quote["mark"] if quote else p["openPriceAvg"]
            sign = 1 if p["posSide"] == "long" else -1
            unrealized += (mark-p["openPriceAvg"])*sign*number(p["total"], True)
            margin += number(p["total"], True)*mark/self.leverage
        equity = self.state["cash_usdt"] + unrealized
        available = max(0.0, equity-margin)
        return {"usdtEquity": ds(equity), "effEquity": ds(equity), "imr": ds(margin),
            "assets": [{"coin": "USDT", "equity": ds(equity), "usdValue": ds(equity),
                "balance": ds(self.state["cash_usdt"]), "available": ds(available), "debt": "0", "locked": "0"}],
            "paper_only": True, "accounting": ACCOUNTING, "funding_accounted": False,
            "maintenance_margin_verified": False, "liquidation_verified": False}

    def settings(self):
        return {"accountMode": "unified", "holdMode": "one_way_mode", "paper_only": True,
            "symbolConfigList": [{"category": CAT, "symbol": symbol, "marginMode": "crossed",
                                 "leverage": str(self.leverage)} for symbol in SYMBOLS]}

    def positions(self, category=CAT):
        return copy.deepcopy(list(self.state["positions"].values())) if category == CAT else []

    def open_orders(self, category=CAT):
        return [copy.deepcopy(row) for row in self.state["orders"].values()
                if category == CAT and row["orderStatus"] in {"live", "new", "partially_filled"}]

    def strategies(self, category=CAT, kind="tpsl"):
        return [copy.deepcopy(row) for row in self.state["stops"].values()
                if category == CAT and kind == "tpsl" and row["status"] == "pending"]

    def inventory(self):
        # Native virtual stops are evaluated before an engine receives its
        # account snapshot; never carry a pre-trigger snapshot into management.
        for symbol in tuple(self.state["positions"]):
            self.quote(symbol)
        assets = self.assets()
        return {"positions": self.positions(), "orders": self.open_orders(), "strategies": self.strategies(),
                "assets": assets, "equity": number(assets["usdtEquity"]), "settings": self.settings(),
                "paper_only": True, **self.scenario_status()}

    entry_snapshot = inventory

    def order(self, oid="", cid=""):
        if not oid and cid:
            oid = self.state["clients"].get(cid, "")
        if str(oid) not in self.state["orders"]:
            raise DataError("PAPER_ORDER_NOT_FOUND")
        return copy.deepcopy(self.state["orders"][str(oid)])

    def fills(self, oid):
        if str(oid) not in self.state["orders"]:
            raise DataError("PAPER_ORDER_NOT_FOUND")
        return copy.deepcopy(self.state["fills"].get(str(oid), []))

    def post(self, path, body):
        if not self.state["valid"]:
            raise SafetyError(GAP_REASON)
        if not isinstance(body, dict) or body.get("category") != CAT:
            raise Rejected("PAPER_ORDER_BODY_INVALID")
        if path == "/api/v3/trade/place-order":
            symbol = body.get("symbol")
            if symbol not in SYMBOLS:
                raise Rejected("PAPER_SYMBOL_INVALID")
            cid = body.get("clientOid")
            if cid in self.state["clients"]:
                original = self.state["orders"][self.state["clients"][cid]]
                if original["request"] != body:
                    raise SafetyError("PAPER_CLIENT_ID_BODY_CONFLICT")
                return {"orderId": original["orderId"], "clientOid": cid, "paper_only": True}
            q = self.quote(symbol)
            response = self._place(body, q)
            self._save()
            return response
        if path == "/api/v3/trade/cancel-order":
            row = self.order(str(body.get("orderId", "")), str(body.get("clientOid", "")))
            if (body.get("symbol") is not None and body["symbol"] != row["symbol"]) or (
                    body.get("clientOid") is not None and body["clientOid"] != row["clientOid"]):
                raise Rejected("PAPER_CANCEL_OWNERSHIP_MISMATCH")
            saved = self.state["orders"][row["orderId"]]
            if saved["orderStatus"] not in {"filled", "cancelled", "rejected"}:
                saved["orderStatus"] = "cancelled"
            self._save()
            return {"orderId": saved["orderId"], "clientOid": saved["clientOid"], "paper_only": True}
        if path in {"/api/v3/trade/place-strategy-order", "/api/v3/trade/modify-strategy-order"}:
            return self._strategy(path, body)
        raise SafetyError("PAPER_MUTATION_ENDPOINT_NOT_ALLOWED")

    def _place(self, body, q, native_stop_id=None):
        symbol, side, cid = body.get("symbol"), body.get("side"), body.get("clientOid")
        if symbol not in SYMBOLS or side not in {"buy", "sell"} or not isinstance(cid, str) or not cid:
            raise Rejected("PAPER_ORDER_IDENTITY_INVALID")
        inst = self.instrument(symbol)
        qty = number(body.get("qty"), True)
        units = Decimal(str(qty))/Decimal(str(inst.qty_step))
        if units != units.to_integral_value() or qty < inst.min_qty or qty > inst.max_qty:
            raise Rejected("PAPER_ORDER_QUANTITY_INVALID")
        kind = body.get("orderType")
        if kind not in {"market", "limit"} or (kind == "limit" and str(body.get("timeInForce", "")).lower() != "ioc"):
            raise Rejected("PAPER_ONLY_MARKET_OR_IOC_SUPPORTED")
        price = (q.ask*(1+self.impact_bps/10000) if side == "buy" else q.bid*(1-self.impact_bps/10000))
        if qty*price < inst.min_value:
            raise Rejected("PAPER_BELOW_MIN_NOTIONAL")
        reduce = str(body.get("reduceOnly", "")).lower() in {"yes", "true"} or body.get("tradeSide") == "close"
        existing = self.state["positions"].get(symbol)
        if reduce:
            if existing is None or side != ("sell" if existing["posSide"] == "long" else "buy"):
                raise Rejected("PAPER_REDUCE_ONLY_POSITION_MISMATCH")
            if qty > number(existing["total"], True) + inst.qty_step/100:
                raise Rejected("PAPER_REDUCE_ONLY_EXCESS_QUANTITY")
        else:
            if existing is not None or len(self.state["positions"]) >= self.c.get("max_positions", 2):
                raise Rejected("PAPER_POSITION_LIMIT")
            stop = number(body.get("stopLoss"), True)
            if (side == "buy" and stop >= q.mark) or (side == "sell" and stop <= q.mark):
                raise Rejected("PAPER_ENTRY_PROTECTION_INVALID")
        oid = self._next("ORDER")
        at = self.now()
        filled = kind == "market" or (price <= number(body.get("price"), True) if side == "buy"
                                      else price >= number(body.get("price"), True))
        fee = self.fee(symbol)
        if not reduce and filled:
            if qty*price > self.c.get("margin_cap_usdt", 300.0)*self.leverage + 1e-8:
                raise Rejected("PAPER_PER_SYMBOL_NOTIONAL_CAP")
            reserve = self.c.get("margin_reserve_usdt", 150.0)
            required = qty*price/self.leverage + qty*price*fee*2 + reserve
            if number(self.assets()["assets"][0]["available"]) < required:
                raise Rejected("PAPER_AVAILABLE_MARGIN_INSUFFICIENT")
        row = {"category": CAT, "symbol": symbol, "orderId": oid, "clientOid": cid,
            "side": side, "qty": ds(qty), "cumExecQty": ds(qty if filled else 0),
            "orderStatus": "filled" if filled else "cancelled", "createdTime": str(at),
            "updatedTime": str(at), "request": copy.deepcopy(body), "paper_only": True,
            "accounting": ACCOUNTING, "fill_model": "ADVERSE_QUOTE_PLUS_ASSUMED_IMPACT",
            "assumed_impact_bps": self.impact_bps, "quote_ts": q.ts}
        self.state["orders"][oid] = row
        self.state["clients"][cid] = oid
        self.state["fills"][oid] = []
        if not filled:
            return {"orderId": oid, "clientOid": cid, "paper_only": True}
        charge = qty*price*fee
        gross = 0.0
        if reduce:
            sign = 1 if existing["posSide"] == "long" else -1
            gross = (price-existing["openPriceAvg"])*sign*qty
            existing["closed_qty"] += qty
            existing["closed_value"] += price*qty
            existing["gross_usdt"] += gross
            existing["exit_fee_usdt"] += charge
            remaining = float(Decimal(existing["total"])-Decimal(str(qty)))
            existing["total"] = ds(max(0, remaining))
            if remaining <= inst.qty_step/100:
                result = {"position_id": existing["positionId"], "closed": at,
                    "exit": existing["closed_value"]/existing["original_qty"],
                    "gross_usdt": existing["gross_usdt"],
                    "fee_cashflow_usdt": -existing["entry_fee_usdt"]-existing["exit_fee_usdt"],
                    "funding_usdt": None, "net_usdt": existing["gross_usdt"]-existing["entry_fee_usdt"]-existing["exit_fee_usdt"],
                    "accounting": ACCOUNTING, "paper_only": True, "funding_accounted": False,
                    "exchange_fills_verified": False, "entry_oid": existing["entry_oid"],
                    "entry_client_oid": existing["entry_client_oid"], "symbol": symbol,
                    "side": existing["posSide"], "opened": existing["createdTime"],
                    "entry": existing["openPriceAvg"], "original_qty": existing["original_qty"],
                    "assumed_impact_bps": self.impact_bps,
                    "fee_observation": copy.deepcopy(self.state["fees"][symbol])}
                self.state["history"][existing["positionId"]] = result
                del self.state["positions"][symbol]
                for stop in self.state["stops"].values():
                    if stop["symbol"] == symbol and stop["status"] == "pending":
                        stop["status"] = "triggered" if stop["orderId"] == native_stop_id else "cancelled"
            else:
                for stop in self.state["stops"].values():
                    if stop["symbol"] == symbol and stop["status"] == "pending":
                        stop["qty"] = existing["total"]
        else:
            position_id = self._next("POSITION")
            existing = {"category": CAT, "symbol": symbol, "posSide": "long" if side == "buy" else "short",
                "marginMode": "crossed", "leverage": str(self.leverage),
                "positionId": position_id, "total": ds(qty), "createdTime": str(at),
                "openPriceAvg": price, "entry_oid": oid, "entry_client_oid": cid,
                "original_qty": qty, "entry_fee_usdt": charge, "exit_fee_usdt": 0.0,
                "closed_qty": 0.0, "closed_value": 0.0, "gross_usdt": 0.0,
                "last_observed_ms": at, "observation_incomplete": False, "paper_only": True}
            self.state["positions"][symbol] = existing
            self._new_stop(existing, number(body["stopLoss"], True), at)
        self.state["cash_usdt"] += gross-charge
        self.state["fills"][oid] = [{"execId": self._next("FILL"), "orderId": oid, "symbol": symbol,
            "execQty": ds(qty), "execPrice": ds(price), "execPnl": ds(gross), "createdTime": str(at),
            "feeDetail": [{"feeCoin": "USDT", "fee": ds(-charge)}], "paper_only": True,
            "accounting": ACCOUNTING}]
        return {"orderId": oid, "clientOid": cid, "paper_only": True}

    def _new_stop(self, position, price, at):
        oid = self._next("STOP")
        row = {"category": CAT, "symbol": position["symbol"], "posSide": position["posSide"],
            "orderId": oid, "status": "pending", "qty": position["total"],
            "stopLoss": ds(price), "slTriggerBy": "mark", "slOrderType": "market",
            "tpslMode": "full", "planType": "pos_loss", "createdTime": str(at),
            "updatedTime": str(at), "paper_only": True, "entry_order_id": position["entry_oid"]}
        self.state["stops"][oid] = row
        return row

    def _strategy(self, path, body):
        symbol = body.get("symbol")
        if symbol not in SYMBOLS or symbol not in self.state["positions"]:
            raise Rejected("PAPER_STOP_POSITION_MISSING")
        p = self.state["positions"][symbol]
        q = self.quote(symbol)
        if symbol not in self.state["positions"]:
            raise Rejected("PAPER_STOP_POSITION_ALREADY_CLOSED")
        price = number(body.get("stopLoss"), True)
        qty = number(body.get("qty"), True)
        if abs(qty-number(p["total"], True)) > self.instrument(symbol).qty_step/100:
            raise Rejected("PAPER_STOP_QUANTITY_MISMATCH")
        if body.get("slTriggerBy", "mark") != "mark" or body.get("slOrderType", "market") != "market":
            raise Rejected("PAPER_STOP_TYPE_INVALID")
        if (p["posSide"] == "long" and price >= q.mark) or (p["posSide"] == "short" and price <= q.mark):
            raise Rejected("PAPER_STOP_ALREADY_CROSSED")
        if path.endswith("modify-strategy-order"):
            oid = str(body.get("orderId", ""))
            row = self.state["stops"].get(oid)
            if row is None or row["status"] != "pending" or row["symbol"] != symbol or row["posSide"] != p["posSide"]:
                raise Rejected("PAPER_STOP_OWNERSHIP_MISMATCH")
            old = number(row["stopLoss"], True)
            if (p["posSide"] == "long" and price < old) or (p["posSide"] == "short" and price > old):
                raise Rejected("PAPER_STOP_CANNOT_LOOSEN")
            row.update({"stopLoss": ds(price), "qty": ds(qty), "updatedTime": str(self.now())})
        else:
            if self.strategies():
                if any(stop["symbol"] == symbol for stop in self.strategies()):
                    raise Rejected("PAPER_PROTECTION_ALREADY_PRESENT")
            row = self._new_stop(p, price, self.now())
        self._save()
        return {"orderId": row["orderId"], "paper_only": True}

    def history_match(self, position, now):
        if not self.state["valid"]:
            return None
        matches = []
        for row in self.state["history"].values():
            if row["symbol"] != position["symbol"] or row["side"] != str(position["side"]).lower():
                continue
            if position.get("entry_oid") and row["entry_oid"] != position["entry_oid"]:
                continue
            if abs(int(row["opened"])-int(position["opened"])) > 10_000:
                continue
            if abs(row["original_qty"]-number(position["original_qty"], True)) > position["qty_step"]/2:
                continue
            if abs(row["entry"]-number(position["entry"], True)) > position["price_step"]*2:
                continue
            matches.append(row)
        if len(matches) > 1:
            raise SafetyError("PAPER_HISTORY_OWNERSHIP_AMBIGUOUS")
        return copy.deepcopy(matches[0]) if matches else None

    def get(self, path, params=None, private=False):
        if path in PUBLIC_PATHS and not private:
            return self.source.get(path, params, private=False)
        if path == "/api/v3/account/fee-rate" and private:
            symbol = (params or {}).get("symbol")
            return {"takerFeeRate": ds(self.fee(symbol)), "paper_only": True,
                    "fee_source": self.state["fees"][symbol]["source"]}
        if not private:
            raise SafetyError("PAPER_READ_ENDPOINT_NOT_ALLOWED")
        if path == "/api/v3/account/info":
            return {"userId": "PAPER_CC_"+self.state["signature"][:16],
                    "permissions": ["uta_trade", "uta_mgt"], "paper_only": True}
        if path == "/api/v3/account/assets":
            return self.assets()
        if path == "/api/v3/account/settings":
            return self.settings()
        raise SafetyError("PAPER_PRIVATE_READ_NOT_ALLOWED")

    def request(self, method, path, params=None, body=None, private=False):
        if method == "GET":
            return self.get(path, params, private)
        if method == "POST" and private and not params:
            return self.post(path, body)
        raise SafetyError("PAPER_REQUEST_NOT_ALLOWED")
