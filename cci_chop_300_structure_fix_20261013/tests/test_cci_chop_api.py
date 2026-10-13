"""Offline transport checks: these tests never contact an exchange."""
import base64
import hashlib
import hmac
import http.client
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from cci_chop_v1._compat.api import WRITES
from cci_chop_v1._compat.core import CAT, DataError, Rejected, SafetyError, UnknownOrder
from cci_chop_v1.api import CCAPI, WRITE_PATHS


CREDS = {"key": "test-key", "secret": "test-secret", "passphrase": "test-passphrase"}
PLACE = "/api/v3/trade/place-order"
MODIFY = "/api/v3/trade/modify-strategy-order"
BODY = {"category": CAT, "symbol": "BTCUSDT", "clientOid": "owned-test", "qty": "0.001"}


class Response:
    def __init__(self, value=None, raw=None):
        self.raw = raw if raw is not None else json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.raw


class CCAPITests(unittest.TestCase):
    def test_read_only_default_rejects_before_network_and_legacy_allowlist_unchanged(self):
        opener = Mock()
        api = CCAPI(creds=CREDS, opener=opener)
        with self.assertRaisesRegex(SafetyError, "READ_ONLY"):
            api.post(PLACE, BODY)
        opener.assert_not_called()
        self.assertNotIn(MODIFY, WRITES)
        self.assertNotIn("/api/v3/trade/place-strategy-order", WRITES)
        self.assertEqual(len(WRITE_PATHS), 4)

    def test_unexpected_route_public_write_and_wrong_scope_reject_before_network(self):
        opener = Mock()
        api = CCAPI(creds=CREDS, write=True, opener=opener)
        for route in ("/api/v3/account/set-leverage", "/api/v3/account/withdrawal", "/api/v2/mix/order/place-order"):
            with self.assertRaisesRegex(SafetyError, "ENDPOINT_NOT_ALLOWED"):
                api.post(route, BODY)
        with self.assertRaisesRegex(SafetyError, "MUST_BE_PRIVATE"):
            api.request("POST", PLACE, body=BODY)
        with self.assertRaisesRegex(SafetyError, "QUERY_NOT_ALLOWED"):
            api.request("POST", PLACE, params={"secret": "ignored"}, body=BODY, private=True)
        for body in ({**BODY, "category": "SPOT"}, {**BODY, "symbol": "UNREVIEWEDUSDT"}):
            with self.assertRaises(SafetyError):
                api.post(PLACE, body)
        opener.assert_not_called()

    def test_signed_private_post_has_exact_serialized_body_and_one_attempt(self):
        opener = Mock(return_value=Response({"code": "00000", "data": {"orderId": "owned-order"}}))
        api = CCAPI(creds=CREDS, write=True, opener=opener)
        with patch("cci_chop_v1.api.now_ms", return_value=1700000000000):
            self.assertEqual(api.post(PLACE, BODY), {"orderId": "owned-order"})
        opener.assert_called_once()
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.bitget.com" + PLACE)
        payload = json.dumps(BODY, separators=(",", ":"), allow_nan=False)
        self.assertEqual(request.data.decode(), payload)
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers["access-key"], CREDS["key"])
        self.assertEqual(headers["access-passphrase"], CREDS["passphrase"])
        self.assertEqual(headers["access-timestamp"], "1700000000000")
        expected = base64.b64encode(hmac.new(
            CREDS["secret"].encode(), ("1700000000000POST" + PLACE + payload).encode(), hashlib.sha256,
        ).digest()).decode()
        self.assertEqual(headers["access-sign"], expected)
        self.assertEqual(opener.call_args.kwargs["timeout"], 5)

    def test_private_get_retains_base_signing_and_read_transport(self):
        opener = Mock(return_value=Response({"code": "00000", "data": {"takerFeeRate": "0.0004"}}))
        api = CCAPI(creds=CREDS, opener=opener)
        with patch("cci_chop_v1._compat.api.now_ms", return_value=1700000000000):
            self.assertEqual(api.fee("BTCUSDT"), .0004)
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        target = "/api/v3/account/fee-rate?category=USDT-FUTURES&symbol=BTCUSDT"
        self.assertEqual(request.full_url, "https://api.bitget.com" + target)
        headers = {k.lower(): v for k, v in request.header_items()}
        expected = base64.b64encode(hmac.new(
            CREDS["secret"].encode(), ("1700000000000GET" + target).encode(), hashlib.sha256,
        ).digest()).decode()
        self.assertEqual(headers["access-sign"], expected)

    def test_demo_requires_explicit_credentials_and_never_loads_live_connection(self):
        with patch("cci_chop_v1.api.connection") as connection:
            with self.assertRaisesRegex(SafetyError, "DEMO_CREDENTIALS_MUST_BE_EXPLICIT"):
                CCAPI(root="unused-live-root", demo=True)
            connection.assert_not_called()

    def test_demo_header_is_present_on_private_get_and_post_without_live_fallback(self):
        read = Mock(return_value=Response({"code": "00000", "data": {"takerFeeRate": ".0004"}}))
        demo = CCAPI(creds=CREDS, opener=read, demo=True)
        self.assertEqual(demo.fee("BTCUSDT"), .0004)
        headers = {key.lower(): value for key, value in read.call_args.args[0].header_items()}
        self.assertEqual(headers["paptrading"], "1")
        self.assertIn("access-sign", headers)

        write = Mock(side_effect=TimeoutError("uncertain demo request"))
        demo = CCAPI(creds=CREDS, opener=write, write=True, demo=True)
        with self.assertRaises(UnknownOrder):
            demo.post(PLACE, BODY)
        write.assert_called_once()
        headers = {key.lower(): value for key, value in write.call_args.args[0].header_items()}
        self.assertEqual(headers["paptrading"], "1")
        self.assertIn("access-sign", headers)

    def test_demo_header_also_applies_to_public_reads_and_normal_default_has_none(self):
        demo_read = Mock(return_value=Response({"code": "00000", "data": []}))
        demo = CCAPI(creds=CREDS, opener=demo_read, demo=True)
        self.assertEqual(demo.get("/api/v3/market/instruments", {"category": CAT}), [])
        headers = {key.lower(): value for key, value in demo_read.call_args.args[0].header_items()}
        self.assertEqual(headers["paptrading"], "1")
        normal_read = Mock(return_value=Response({"code": "00000", "data": []}))
        CCAPI(opener=normal_read).get("/api/v3/market/instruments", {"category": CAT})
        headers = {key.lower(): value for key, value in normal_read.call_args.args[0].header_items()}
        self.assertNotIn("paptrading", headers)

    def test_transport_timeout_for_entry_and_stop_modify_is_unknown_and_never_retried(self):
        for path, body in ((PLACE, BODY), (MODIFY, {**BODY, "orderId": "stop-id", "stopLoss": "99000"})):
            for error in (TimeoutError("test-key test-secret"), urllib.error.URLError("test-passphrase"),
                          http.client.IncompleteRead(b"test-secret", 200)):
                with self.subTest(path=path, error=type(error).__name__):
                    opener = Mock(side_effect=error)
                    api = CCAPI(creds=CREDS, write=True, opener=opener)
                    with self.assertRaises(UnknownOrder) as raised:
                        api.post(path, body)
                    opener.assert_called_once()
                    for value in CREDS.values():
                        self.assertNotIn(value, str(raised.exception))

    def test_returned_rejection_vs_ambiguous_and_malformed_ack(self):
        cases = [
            ({"code": "40017", "msg": "test-secret", "data": None}, Rejected),
            ({"code": "40010", "msg": "timeout", "data": None}, UnknownOrder),
            ({"code": "40725", "data": None}, UnknownOrder),
            ({"code": "45001", "data": None}, UnknownOrder),
            ({"code": "00000"}, UnknownOrder),
            ({"code": "00000", "data": None}, UnknownOrder),
            ({"code": "00000", "data": {}}, UnknownOrder),
            ({"code": "test-secret", "data": {}}, UnknownOrder),
            ([], UnknownOrder),
        ]
        for result, error in cases:
            with self.subTest(result=result):
                opener = Mock(return_value=Response(result))
                api = CCAPI(creds=CREDS, write=True, opener=opener)
                with self.assertRaises(error) as raised:
                    api.post(PLACE, BODY)
                self.assertNotIn("test-secret", str(raised.exception))
                opener.assert_called_once()
        opener = Mock(return_value=Response(raw=b"not-json test-secret"))
        with self.assertRaises(UnknownOrder):
            CCAPI(creds=CREDS, write=True, opener=opener).post(PLACE, BODY)
        opener.assert_called_once()

    def test_http_error_exchange_code_is_classified_without_body_or_secret_logging(self):
        for code, error_type in (("40017", Rejected), ("40010", UnknownOrder)):
            raw = json.dumps({"code": code, "msg": "test-secret", "data": None}).encode()
            error = urllib.error.HTTPError("https://api.bitget.com", 400, "test-passphrase", {}, io.BytesIO(raw))
            opener = Mock(side_effect=error)
            with self.assertRaises(error_type) as raised:
                CCAPI(creds=CREDS, write=True, opener=opener).post(PLACE, BODY)
            self.assertNotIn("test-secret", str(raised.exception))
            self.assertNotIn("test-passphrase", str(raised.exception))
            opener.assert_called_once()

    def test_modify_requires_positive_qty_and_order_id_before_transport(self):
        opener = Mock()
        api = CCAPI(creds=CREDS, write=True, opener=opener)
        for qty in (None, "0", "-1", "nan", "inf"):
            with self.assertRaises(SafetyError):
                api.post(MODIFY, {**BODY, "orderId": "stop-id", "qty": qty})
        with self.assertRaisesRegex(SafetyError, "ORDER_ID_REQUIRED"):
            api.post(MODIFY, BODY)
        opener.assert_not_called()

    def test_modify_local_scope_is_removed_from_signed_exchange_body(self):
        opener = Mock(return_value=Response({"code": "00000", "data": {"orderId": "stop-id"}}))
        api = CCAPI(creds=CREDS, write=True, opener=opener)
        body = {"category": CAT, "symbol": "BTCUSDT", "orderId": "stop-id", "qty": ".001",
                "stopLoss": "99000", "slTriggerBy": "mark", "slOrderType": "market"}
        with patch("cci_chop_v1.api.now_ms", return_value=1700000000000):
            api.post(MODIFY, body)
        request = opener.call_args.args[0]
        sent = json.loads(request.data)
        self.assertEqual(sent, {key: value for key, value in body.items() if key not in ("category", "symbol")})
        self.assertEqual(body["symbol"], "BTCUSDT")
        self.assertEqual(body["category"], CAT)
        headers = {key.lower(): value for key, value in request.header_items()}
        expected = base64.b64encode(hmac.new(
            CREDS["secret"].encode(), ("1700000000000POST" + MODIFY + request.data.decode()).encode(), hashlib.sha256,
        ).digest()).decode()
        self.assertEqual(headers["access-sign"], expected)

    def test_strategies_uses_documented_array_without_invented_pagination(self):
        api = CCAPI()
        api.get = Mock(return_value=[{"orderId": "stop-id", "status": "pending"}])
        result = api.strategies(CAT, "tpsl")
        self.assertEqual(result[0]["orderId"], "stop-id")
        api.get.assert_called_once_with(
            "/api/v3/trade/unfilled-strategy-orders", {"category": CAT, "type": "tpsl"}, private=True,
        )
        for malformed in ({"list": []}, None, [{"orderId": "valid"}, "malformed"]):
            api.get = Mock(return_value=malformed)
            with self.assertRaises(DataError):
                api.strategies()
        api.get = Mock(return_value=[])
        self.assertEqual(api.strategies(), [])

    def test_personal_fee_has_no_public_or_default_fallback(self):
        api = CCAPI()
        for malformed in ({}, None, {"takerFeeRate": "nan"}, {"takerFeeRate": "-0.0004"}, {"takerFeeRate": ".1"}):
            api.get = Mock(return_value=malformed)
            with self.assertRaises(DataError):
                api.fee("ETHUSDT")
        api.get = Mock(side_effect=DataError("read failed"))
        with self.assertRaises(DataError):
            api.fee("BTCUSDT")
        api.get.assert_called_once()

    def test_native_child_reads_use_owned_parent_and_bounded_complete_pages(self):
        api = CCAPI()
        first = [{"subOrderId": "child-" + str(i)} for i in range(100)]
        last = [{"subOrderId": "child-last"}]
        api.get = Mock(side_effect=[{"list": first, "cursor": 123}, {"list": last, "cursor": 1}])
        self.assertEqual(len(api.strategy_sub_orders("owned-parent")), 101)
        self.assertEqual(api.get.call_args_list[0].args,
                         ("/api/v3/trade/strategy-sub-orders", {"orderId": "owned-parent", "limit": "100"}))
        self.assertEqual(api.get.call_args_list[1].args[1]["cursor"], "123")
        self.assertTrue(all(call.kwargs == {"private": True} for call in api.get.call_args_list))

    def test_native_child_malformed_missing_duplicate_and_loop_responses_are_not_empty(self):
        api = CCAPI()
        for data in (None, [], {"list": None}, {"list": ["bad"]}, {"list": [{}]},
                     {"list": [{"subOrderId": "same"}, {"subOrderId": "same"}]}):
            api.get = Mock(return_value=data)
            with self.assertRaises(DataError):
                api.strategy_sub_orders("owned-parent")
        first = [{"subOrderId": "first-" + str(i)} for i in range(100)]
        second = [{"subOrderId": "second-" + str(i)} for i in range(100)]
        api.get = Mock(side_effect=[{"list": first, "cursor": 123}, {"list": second, "cursor": "123"}])
        with self.assertRaisesRegex(DataError, "PAGINATION_INCOMPLETE"):
            api.strategy_sub_orders("owned-parent")
        api.get = Mock()
        for invalid in (None, "", True, 1):
            with self.assertRaises(DataError):
                api.strategy_sub_orders(invalid)
        api.get.assert_not_called()

    def test_native_child_large_history_hits_page_bound_without_write(self):
        api = CCAPI()
        api.get = Mock(side_effect=[{"list": [{"subOrderId": str(page) + "-" + str(i)} for i in range(100)],
                                    "cursor": page + 1} for page in range(10)])
        with self.assertRaisesRegex(DataError, "PAGINATION_LIMIT"):
            api.strategy_sub_orders("owned-parent")
        self.assertEqual(api.get.call_count, 10)

    def test_inventory_and_history_match_keep_existing_read_only_reconciliation(self):
        api = CCAPI()
        with patch("cci_chop_v1.api.account.inventory", return_value={"positions": []}) as inventory:
            self.assertEqual(api.inventory(), {"positions": []})
            inventory.assert_called_once_with(api)
        position = {"symbol": "BTCUSDT"}
        with patch("cci_chop_v1.api.account.history_match", return_value=None) as history:
            self.assertIsNone(api.history_match(position, 1000000))
            history.assert_called_once_with(api, position, 1000000)


if __name__ == "__main__":
    unittest.main()
