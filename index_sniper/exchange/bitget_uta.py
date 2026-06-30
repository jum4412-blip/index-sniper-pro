from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import requests


class BitgetUTAError(RuntimeError):
    pass


class BitgetUTAClient:
    """Small Bitget UTA v3 REST client.

    v0.2 rule: methods that can place orders exist, but `main.py --mode dry-order`
    never calls them. Real order execution will be added only after an explicit live probe step.
    """

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

    def get(self, path: str, params: dict[str, Any] | None = None, *, private: bool = True) -> dict[str, Any]:
        full_path = path + self._query(params)
        ts = self._timestamp()
        headers = self._headers(ts, "GET", full_path) if private else {"Content-Type": "application/json", "locale": "en-US"}
        r = requests.get(self.BASE_URL + full_path, headers=headers, timeout=self.timeout)
        return self._parse(r)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        ts = self._timestamp()
        r = requests.post(self.BASE_URL + path, headers=self._headers(ts, "POST", path, body), data=body, timeout=self.timeout)
        return self._parse(r)

    def _parse(self, response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception as exc:
            raise BitgetUTAError(f"Non-JSON response HTTP {response.status_code}: {response.text[:300]}") from exc
        if response.status_code >= 400:
            raise BitgetUTAError(f"HTTP {response.status_code}: {data}")
        return data

    @staticmethod
    def ok(data: dict[str, Any]) -> bool:
        return str(data.get("code")) in {"00000", "0"}

    def account_info(self) -> dict[str, Any]:
        return self.get("/api/v3/account/info")

    def assets(self) -> dict[str, Any]:
        return self.get("/api/v3/account/assets")

    def settings(self) -> dict[str, Any]:
        return self.get("/api/v3/account/settings")

    def instruments(self, category: str = "USDT-FUTURES", symbol: str | None = None) -> dict[str, Any]:
        return self.get("/api/v3/market/instruments", {"category": category, "symbol": symbol}, private=False)

    def ticker(self, symbol: str, category: str = "USDT-FUTURES") -> dict[str, Any]:
        return self.get("/api/v3/market/tickers", {"category": category, "symbol": symbol}, private=False)

    def ticker_last_price(self, symbol: str, category: str = "USDT-FUTURES") -> float | None:
        data = self.ticker(symbol, category)
        if not self.ok(data):
            return None
        items = data.get("data") or []
        if isinstance(items, dict):
            items = [items]
        for item in items:
            if item.get("symbol") == symbol or len(items) == 1:
                value = item.get("lastPrice") or item.get("last") or item.get("price")
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None

    def positions(self, category: str = "USDT-FUTURES", symbol: str | None = None, pos_side: str | None = None) -> dict[str, Any]:
        return self.get("/api/v3/position/current-position", {"category": category, "symbol": symbol, "posSide": pos_side})

    def open_orders(self, category: str = "USDT-FUTURES", symbol: str | None = None) -> dict[str, Any]:
        return self.get("/api/v3/trade/unfilled-orders", {"category": category, "symbol": symbol})

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/api/v3/trade/place-order", payload)

    def order_info(self, order_id: str | None = None, client_oid: str | None = None) -> dict[str, Any]:
        return self.get("/api/v3/trade/order-info", {"orderId": order_id, "clientOid": client_oid})
