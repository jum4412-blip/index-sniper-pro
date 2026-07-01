from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests


class BitgetUTAError(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    pos_side: str
    qty: str
    category: str = "USDT-FUTURES"
    margin_coin: str = "USDT"
    margin_mode: str = "crossed"
    order_type: str = "market"
    reduce_only: bool = False
    client_oid: str | None = None
    take_profit: str | None = None
    stop_loss: str | None = None
    tp_trigger_by: str = "market"
    sl_trigger_by: str = "market"
    tp_order_type: str = "market"
    sl_order_type: str = "market"


class BitgetUTAClient:
    BASE_URL = "https://api.bitget.com"

    def __init__(self, api_key: str, secret_key: str, passphrase: str, timeout: int = 10):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.timeout = timeout

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = ts + method.upper() + path + body
        mac = hmac.new(self.secret_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _headers(self, ts: str, method: str, path: str, body: str = "") -> dict[str, str]:
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    @staticmethod
    def _query(params: dict[str, Any] | None) -> str:
        if not params:
            return ""
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        return "?" + urlencode(clean) if clean else ""

    def get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = True) -> dict[str, Any]:
        full_path = path + self._query(params)
        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if auth:
            ts = self._timestamp()
            headers = self._headers(ts, "GET", full_path)
        response = requests.get(self.BASE_URL + full_path, headers=headers, timeout=self.timeout)
        return self._parse(response)

    def post(self, path: str, payload: dict[str, Any], *, auth: bool = True) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if auth:
            ts = self._timestamp()
            headers = self._headers(ts, "POST", path, body)
        response = requests.post(self.BASE_URL + path, headers=headers, data=body, timeout=self.timeout)
        return self._parse(response)

    @staticmethod
    def _parse(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception as exc:
            raise BitgetUTAError(f"Non-JSON HTTP {response.status_code}: {response.text[:300]}") from exc
        if response.status_code >= 400:
            raise BitgetUTAError(f"HTTP {response.status_code}: {data}")
        return data

    @staticmethod
    def is_success(data: dict[str, Any]) -> bool:
        return str(data.get("code")) in {"00000", "0"}

    def account_info(self) -> dict[str, Any]:
        return self.get("/api/v3/account/info")

    def assets(self) -> dict[str, Any]:
        return self.get("/api/v3/account/assets")

    def settings(self) -> dict[str, Any]:
        return self.get("/api/v3/account/settings")

    def tickers(self, symbol: str | None = None, category: str = "USDT-FUTURES") -> dict[str, Any]:
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        return self.get("/api/v3/market/tickers", params, auth=False)

    def instruments(self, symbol: str | None = None, category: str = "USDT-FUTURES") -> dict[str, Any]:
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        return self.get("/api/v3/market/instruments", params, auth=False)

    def current_position(self, symbol: str | None = None, category: str = "USDT-FUTURES") -> dict[str, Any]:
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        return self.get("/api/v3/position/current-position", params)

    def candles(self, symbol: str, category: str = "USDT-FUTURES", interval: str = "1D", limit: int = 100, candle_type: str = "market") -> dict[str, Any]:
        params: dict[str, Any] = {"category": category, "symbol": symbol, "interval": interval, "type": candle_type, "limit": str(limit)}
        return self.get("/api/v3/market/candles", params, auth=False)

    def last_price(self, symbol: str, category: str = "USDT-FUTURES") -> float:
        data = self.tickers(symbol=symbol, category=category)
        if not self.is_success(data):
            raise BitgetUTAError(f"ticker failed for {symbol}: {data}")
        payload = data.get("data")
        if isinstance(payload, list):
            row = payload[0] if payload else None
        elif isinstance(payload, dict):
            rows = payload.get("list")
            row = rows[0] if isinstance(rows, list) and rows else payload
        else:
            row = None
        if not isinstance(row, dict):
            raise BitgetUTAError(f"ticker empty for {symbol}: {data}")
        for key in ("lastPrice", "lastPr", "last", "close", "price", "markPrice"):
            if row.get(key) not in (None, ""):
                return float(row[key])
        raise BitgetUTAError(f"ticker missing price field for {symbol}: {row}")

    @staticmethod
    def build_market_order_payload(intent: OrderIntent) -> dict[str, Any]:
        body: dict[str, Any] = {
            "symbol": intent.symbol,
            "category": intent.category,
            "marginCoin": intent.margin_coin,
            "marginMode": intent.margin_mode,
            "orderType": intent.order_type,
            "side": intent.side,
            "posSide": intent.pos_side,
            "qty": str(intent.qty),
        }
        if intent.client_oid:
            body["clientOid"] = intent.client_oid
        # Bitget UTA hedge-mode close logic:
        # - Close long  = side=sell + posSide=long
        # - Close short = side=buy  + posSide=short
        # UTA rejects orders that send posSide and reduceOnly together in hedge-mode
        # (error 25238). Our account uses hedge-mode, so close intents keep posSide
        # and intentionally omit reduceOnly. In one-way mode, posSide would be blank
        # and reduceOnly can be sent.
        if intent.reduce_only and not intent.pos_side:
            body["reduceOnly"] = "yes"
        if not intent.reduce_only:
            if intent.take_profit:
                body["takeProfit"] = str(intent.take_profit)
                body["tpTriggerBy"] = intent.tp_trigger_by
                body["tpOrderType"] = intent.tp_order_type
            if intent.stop_loss:
                body["stopLoss"] = str(intent.stop_loss)
                body["slTriggerBy"] = intent.sl_trigger_by
                body["slOrderType"] = intent.sl_order_type
        return body

    def place_order(self, intent: OrderIntent, *, dry_run: bool = True) -> dict[str, Any]:
        payload = self.build_market_order_payload(intent)
        if dry_run:
            return {"dry_run": True, "payload": payload}
        return self.post("/api/v3/trade/place-order", payload)
