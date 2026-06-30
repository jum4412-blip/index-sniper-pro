import base64
import hashlib
import hmac
import json
import time
from typing import Any

import requests


class BitgetUTAError(RuntimeError):
    pass


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

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        full_path = path + query
        ts = self._timestamp()
        r = requests.get(self.BASE_URL + full_path, headers=self._headers(ts, "GET", full_path), timeout=self.timeout)
        return self._parse(r)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"))
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

    def account_info(self) -> dict[str, Any]:
        return self.get("/api/v3/account/info")

    def assets(self) -> dict[str, Any]:
        return self.get("/api/v3/account/assets")

    def settings(self) -> dict[str, Any]:
        return self.get("/api/v3/account/settings")

    def ticker(self, symbol: str, category: str = "USDT-FUTURES") -> dict[str, Any]:
        # UTA market ticker endpoint may differ by product availability. This method tries v3 first.
        return self.get("/api/v3/market/tickers", {"category": category, "symbol": symbol})
