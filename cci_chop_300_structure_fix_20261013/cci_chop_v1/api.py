"""Scoped Bitget UTA adapter for the multi-timeframe structural-stop release.

Read helpers retain the established account adapter.  The four write routes
below belong only to this adapter; ``basic_core.api.WRITES`` is never expanded.
Every POST has one transport attempt, including native-stop modifications.
An uncertain result must be reconciled from exchange reads before any resend.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from cci_chop_v1._compat import account
from cci_chop_v1._compat.api import Rest, credentials, rows
from cci_chop_v1._compat.core import CAT, SYMBOLS, DataError, Rejected, SafetyError, UnknownOrder, now_ms, number
from cci_chop_v1._compat.runtime import connection


WRITE_PATHS = frozenset({
    "/api/v3/trade/place-order",
    "/api/v3/trade/cancel-order",
    "/api/v3/trade/place-strategy-order",
    "/api/v3/trade/modify-strategy-order",
})
AMBIGUOUS_CODES = frozenset({"40010", "40725", "45001"})
STRATEGY_TYPES = frozenset({"tpsl", "trigger", "oco", "trailing_stop", "iceberg", "twap"})
ACCOUNT_CATEGORIES = frozenset({CAT, "USDC-FUTURES", "COIN-FUTURES", "SPOT", "MARGIN"})


class CCAPI(Rest):
    """API keys stay local; construction and all reads are read-only by default.

    ``root`` resolves the existing connection.json/env file.  ``creds`` and
    ``opener`` are explicit injection points for offline execution tests.
    Live mode must explicitly pass ``write=True`` after the runner's arm checks.
    """

    def __init__(self, root=None, write=False, opener=None, creds=None, demo=False):
        if not isinstance(write, bool):
            raise SafetyError("CC_WRITE_FLAG_INVALID")
        if not isinstance(demo, bool):
            raise SafetyError("CC_DEMO_FLAG_INVALID")
        if demo and creds is None:
            # Demo keys must be supplied by the separate demo-env CLI path.
            # Never implicitly reuse connection.json's live credentials.
            raise SafetyError("CC_DEMO_CREDENTIALS_MUST_BE_EXPLICIT")
        if creds is None and root is not None:
            creds = credentials(connection(Path(root))["env"])
        self.demo = demo
        transport = opener or urllib.request.urlopen
        if demo:
            # Official UTA demo REST guide requires a separate Demo API Key
            # and paptrading:1 for its API calls.  This applies to GET and POST
            # without changing the HMAC string.  A failure never falls back to
            # a request without the demo header.
            # https://www.bitget.com/docs/uta/demo-trading/rest-api
            demo_transport = transport
            def demo_opener(request, **kwargs):
                request.add_header("paptrading", "1")
                return demo_transport(request, **kwargs)
            transport = demo_opener
        super().__init__(creds=creds, write=write, opener=transport)

    def request(self, method, path, params=None, body=None, private=False):
        if method == "POST":
            if params:
                raise SafetyError("CC_POST_QUERY_NOT_ALLOWED")
            if not private:
                raise SafetyError("CC_POST_MUST_BE_PRIVATE")
            return self._request_post(path, body)
        return super().request(method, path, params=params, body=body, private=private)

    def post(self, path, body):
        return self.request("POST", path, body=body, private=True)

    def _write_payload(self, path, body):
        # Gate all paths before credentials, serialization, or transport.
        if not self.write:
            raise SafetyError("CC_API_READ_ONLY")
        if path not in WRITE_PATHS:
            raise SafetyError("CC_WRITE_ENDPOINT_NOT_ALLOWED")
        if not isinstance(body, dict) or body.get("category") != CAT:
            raise SafetyError("CC_WRITE_CATEGORY_NOT_ALLOWED")
        symbol = body.get("symbol")
        if path != "/api/v3/trade/cancel-order" and symbol not in SYMBOLS:
            raise SafetyError("CC_WRITE_SYMBOL_NOT_ALLOWED")
        if symbol is not None and symbol not in SYMBOLS:
            raise SafetyError("CC_WRITE_SYMBOL_NOT_ALLOWED")
        if path == "/api/v3/trade/modify-strategy-order":
            if not body.get("orderId"):
                raise SafetyError("CC_MODIFY_ORDER_ID_REQUIRED")
            # Bitget requires qty when modifying a TPSL order.  Never infer it
            # from local size or silently modify protection for zero quantity.
            number(body.get("qty"), True)
        if not isinstance(self.creds, dict) or any(
            not isinstance(self.creds.get(key), str) or not self.creds[key]
            for key in ("key", "secret", "passphrase")
        ):
            raise SafetyError("CC_PRIVATE_CREDENTIALS_MISSING")
        if path == "/api/v3/trade/modify-strategy-order":
            # Symbol/category are local scope checks only.  Unlike placement,
            # the documented modification body identifies the bound strategy
            # by orderId and does not accept these two placement parameters.
            body = {key: value for key, value in body.items() if key not in ("category", "symbol")}
        try:
            return json.dumps(body, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError):
            raise SafetyError("CC_WRITE_BODY_INVALID") from None

    @staticmethod
    def _post_result(raw, http_error=False):
        try:
            result = json.loads(raw)
        except (ValueError, TypeError, UnicodeError):
            raise UnknownOrder("CC_WRITE_RESPONSE_UNKNOWN: reconcile; do not resend") from None
        if not isinstance(result, dict) or not isinstance(result.get("code"), (str, int)):
            raise UnknownOrder("CC_WRITE_ENVELOPE_UNKNOWN: reconcile; do not resend")
        code = str(result["code"])
        # Error messages and raw bodies may contain account information.  Only
        # a conventional numeric Bitget code is ever included in an exception.
        if not re.fullmatch(r"[0-9]{5}", code):
            raise UnknownOrder("CC_WRITE_CODE_UNKNOWN: reconcile; do not resend")
        if code != "00000":
            if code in AMBIGUOUS_CODES:
                raise UnknownOrder("CC_WRITE_OUTCOME_UNKNOWN: exchange code " + code)
            raise Rejected("CC_WRITE_REJECTED: exchange code " + code)
        if http_error or not isinstance(result.get("data"), dict) or not result["data"]:
            raise UnknownOrder("CC_WRITE_ACK_UNKNOWN: reconcile; do not resend")
        return result["data"]

    def _request_post(self, path, body):
        payload = self._write_payload(path, body)
        with self.lock:
            gap = self.next_request - time.monotonic()
            if gap > 0:
                time.sleep(gap)
            self.next_request = time.monotonic() + .16
        stamp = str(now_ms())
        signature = base64.b64encode(hmac.new(
            self.creds["secret"].encode(),
            (stamp + "POST" + path + payload).encode(), hashlib.sha256,
        ).digest()).decode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "MTFStructure/1.0",
            "ACCESS-KEY": self.creds["key"],
            "ACCESS-PASSPHRASE": self.creds["passphrase"],
            "ACCESS-TIMESTAMP": stamp,
            "ACCESS-SIGN": signature,
        }
        request = urllib.request.Request(
            "https://api.bitget.com" + path,
            data=payload.encode(), headers=headers, method="POST",
        )
        # Exactly one call to opener.  A failed modification is as ambiguous as
        # a failed entry, and therefore also has no automatic retry.
        try:
            with self.opener(request, timeout=5) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except (OSError, ValueError, TimeoutError, http.client.HTTPException):
                raise UnknownOrder("CC_WRITE_TRANSPORT_UNKNOWN: reconcile; do not resend") from None
            finally:
                exc.close()
            return self._post_result(raw, http_error=True)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
            raise UnknownOrder("CC_WRITE_TRANSPORT_UNKNOWN: reconcile; do not resend") from None
        return self._post_result(raw)

    def strategies(self, category=CAT, kind="tpsl"):
        if category not in ACCOUNT_CATEGORIES or kind not in STRATEGY_TYPES:
            raise DataError("CC_STRATEGY_QUERY_INVALID")
        path = "/api/v3/trade/unfilled-strategy-orders"
        data = self.get(path, {"category": category, "type": kind}, private=True)
        # UTA documents this response as a direct array.  Sending invented
        # cursor/limit parameters or treating a malformed object as [] would
        # make an account ownership check incorrectly appear complete.
        if not isinstance(data, list):
            raise DataError("CC_STRATEGY_ARRAY_REQUIRED")
        return rows(data, path)

    def fee(self, symbol):
        if symbol not in SYMBOLS:
            raise DataError("CC_FEE_SYMBOL_INVALID")
        data = self.get("/api/v3/account/fee-rate", {"category": CAT, "symbol": symbol}, private=True)
        if not isinstance(data, dict):
            raise DataError("CC_PERSONAL_FEE_MISSING")
        fee = number(data.get("takerFeeRate"))
        if not 0 <= fee < .01:
            raise DataError("CC_PERSONAL_FEE_INVALID")
        return fee

    def strategy_sub_orders(self, strategy_order_id):
        """Read every bounded page for an already owned native stop parent.

        This endpoint does not place, replace, or cancel an order.  A malformed
        or incomplete response never means that the native stop has no children.
        The execution engine separately checks ownership and actual fill rows.
        """
        if (not isinstance(strategy_order_id, str) or not strategy_order_id
                or len(strategy_order_id) > 128):
            raise DataError("CC_NATIVE_PARENT_ID_REQUIRED")
        path = "/api/v3/trade/strategy-sub-orders"
        found, seen_ids, seen_cursors = [], set(), set()
        cursor = None
        for _ in range(10):
            params = {"orderId": strategy_order_id, "limit": "100"}
            if cursor is not None:
                params["cursor"] = cursor
            data = self.get(path, params, private=True)
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise DataError("CC_NATIVE_CHILD_LIST_REQUIRED")
            batch = rows(data, path)
            if len(batch) > 100:
                raise DataError("CC_NATIVE_CHILD_PAGE_TOO_LARGE")
            for row in batch:
                child_id = row.get("subOrderId")
                if not isinstance(child_id, str) or not child_id or child_id in seen_ids:
                    raise DataError("CC_NATIVE_CHILD_ID_MISSING_OR_DUPLICATED")
                seen_ids.add(child_id)
                found.append(row)
            if len(batch) < 100:
                return found
            value = data.get("cursor")
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise DataError("CC_NATIVE_CHILD_PAGINATION_INCOMPLETE")
            cursor = str(value)
            if not cursor or cursor in ("0", "-1") or cursor in seen_cursors:
                raise DataError("CC_NATIVE_CHILD_PAGINATION_INCOMPLETE")
            seen_cursors.add(cursor)
        raise DataError("CC_NATIVE_CHILD_PAGINATION_LIMIT")

    def inventory(self):
        return account.inventory(self)

    def history_match(self, position, now):
        return account.history_match(self, position, now)
