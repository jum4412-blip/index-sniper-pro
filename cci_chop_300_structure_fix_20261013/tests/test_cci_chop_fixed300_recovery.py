"""Offline regressions for fixed margin and verified-flat recovery.

No exchange clients, credentials, production state, or network are used here.
The fake reproduces the parent strategy response containing its OPEN order
alongside native CLOSE orders that caused the reported production halt.
"""
import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cci_chop_v1._compat.core import CAT, DataError, rounded
from cci_chop_v1._compat.store import Store
from cci_chop_v1.engine import CCEngine, _period
from test_cci_chop_engine import FakeAPI, NOW, gate


class Fixed300RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite")
        self.api = FakeAPI(self.store)
        self.config = {
            "symbols": ["BTCUSDT", "ETHUSDT"], "leverage": 5,
            "sizing_policy": "fixed_margin", "target_margin_usdt": 300.,
            "margin_cap_usdt": 300., "max_positions": 2,
            "risk_fraction": .0001, "max_trade_risk_usdt": 1.,
            "max_portfolio_risk_fraction": .0002,
        }
        self.engine = CCEngine(self.api, self.store, self.config,
                               lambda: True, clock=lambda: self.api.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def signal(self, symbol="BTCUSDT", side="LONG"):
        return {"symbol": symbol, "side": side,
                "stop": 9700. if side == "LONG" else 10300.,
                "entry": 10000., "signal_ts": NOW - 60000,
                "signal_id": "fixed300-" + symbol + side + str(NOW)}

    def open(self, symbol="BTCUSDT", side="LONG"):
        self.engine.tick(NOW, [self.signal(symbol, side)], {symbol: gate(side)})
        self.api.now = NOW + 1000
        self.engine.tick(self.api.now)
        return self.engine.state["positions"][symbol]

    def history(self, p, net=-9.5715268):
        return {
            "accounting": "EXCHANGE_POSITION_HISTORY_VERIFIED",
            "position_id": p.get("exchange_position_id", "p-" + p["entry_oid"]),
            "closed": self.api.now, "exit": p["initial_stop"],
            "net_usdt": net, "gross_usdt": net + 1.,
            "fee_cashflow_usdt": -.8, "funding_usdt": -.2,
        }

    def entry_child(self, p):
        return {"subOrderId": p["entry_oid"], "subClientOid": p["entry_cid"],
                "category": CAT, "symbol": p["symbol"],
                "posSide": p["side"].lower(),
                "side": "buy" if p["side"] == "LONG" else "sell",
                "qty": str(p.get("requested_qty", p["original_qty"])),
                "cumExecQty": str(p["original_qty"]), "status": "filled",
                "createdTime": str(p["opened"])}

    def close_child(self, p, status="live", cumulative=0.):
        return {"subOrderId": "native-close-1", "subClientOid": "native-client-1",
                "category": CAT, "symbol": p["symbol"],
                "posSide": p["side"].lower(),
                "side": "sell" if p["side"] == "LONG" else "buy",
                "qty": str(p["original_qty"]), "cumExecQty": str(cumulative),
                "status": status, "createdTime": str(NOW + 2000)}

    def test_fixed_300_is_not_shrunk_by_one_percent_or_twelve_dollar_limits(self):
        p = self.open()
        body = self.api.writes[0][1]
        expected = rounded(1500. / float(body["price"]), p["qty_step"])
        self.assertEqual(float(body["qty"]), expected)
        self.assertEqual(body["timeInForce"], "fok")
        self.assertGreater(p["initial_risk_usdt"], 12.)
        self.assertGreater(p["planned_risk_usdt"], 1042.19 * .01)
        self.assertIsNone(self.engine.state["halt"])
        self.assertNotIn("force_exit", p)
        self.assertEqual(p["sizing_policy"], "fixed_margin")
        self.assertEqual(p["target_margin_usdt"], 300.)
        self.assertEqual(p["target_notional_usdt"], 1500.)
        self.assertEqual(p["requested_qty"], expected)
        self.assertLessEqual(p["margin_at_limit_usdt"], 300.)
        self.assertLess(300. - p["margin_at_limit_usdt"],
                        p["qty_step"] * float(body["price"]) / 5)

    def test_long_and_short_preserve_structural_guard_without_fixed_tp(self):
        p = self.open()
        body = self.api.writes[0][1]
        self.assertEqual(float(body["stopLoss"]), 9700.)
        self.assertNotIn("takeProfit", body)
        self.api.mark = 9850.  # beyond a fixed 1% price decline, above structure.
        self.api.now += 1000
        self.engine.tick(self.api.now)
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(p["exchange_stop"], 9700.)
        self.api.mark = 10200.  # profits alone cannot trigger a fixed target.
        self.api.now += 1000
        self.engine.tick(self.api.now)
        self.assertEqual(len(self.api.writes), 1)
        self.engine.tick(self.api.now, exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        self.assertEqual(len(self.api.writes), 2)
        self.assertEqual(self.api.writes[-1][1]["side"], "sell")
        self.assertEqual(p["exit_reason"], "STRUCTURE_REVERSAL")

    def test_fixed_short_quantity_uses_same_300_target_and_original_structure(self):
        p = self.open(side="SHORT")
        body = self.api.writes[0][1]
        self.assertEqual(body["side"], "sell")
        self.assertEqual(body["timeInForce"], "fok")
        self.assertEqual(float(body["stopLoss"]), 10300.)
        self.assertEqual(float(body["qty"]), rounded(1500. / float(body["price"]), .001))
        self.assertNotIn("force_exit", p)
        self.assertIsNone(self.engine.state["halt"])

    def test_exchange_maximum_does_not_silently_resize_fixed_order(self):
        instrument = self.api.instrument
        self.api.instrument = lambda symbol: replace(instrument(symbol), max_qty=.1)
        self.engine.tick(NOW, [self.signal()], gate())
        self.assertEqual(self.api.writes, [])
        self.assertTrue(self.engine.state["halt"])

    def test_exchange_minimum_does_not_round_fixed_order_up(self):
        instrument = self.api.instrument
        self.api.instrument = lambda symbol: replace(instrument(symbol), min_qty=.2)
        self.engine.tick(NOW, [self.signal()], gate())
        self.assertEqual(self.api.writes, [])
        self.assertTrue(self.engine.state["halt"])

    def test_insufficient_collateral_skips_instead_of_small_entry(self):
        original = self.api.inventory
        def limited_collateral():
            snap = original()
            snap["assets"]["assets"][0]["available"] = "300"
            return snap
        self.api.inventory = limited_collateral
        self.engine.tick(NOW, [self.signal()], gate())
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.engine.state["halt"],
                         "FREE_COLLATERAL_WITH_RESERVE_INSUFFICIENT")

    def test_btc_and_eth_each_target_300_and_two_position_cap_remains(self):
        self.engine.tick(NOW, [self.signal("BTCUSDT"), self.signal("ETHUSDT")],
                         {"BTCUSDT": gate(), "ETHUSDT": gate()})
        self.assertEqual(len(self.api.writes), 2)
        self.assertEqual(set(self.engine.state["positions"]), {"BTCUSDT", "ETHUSDT"})
        for p in self.engine.state["positions"].values():
            self.assertEqual(p["target_margin_usdt"], 300.)
            self.assertGreater(p["initial_risk_usdt"], 12.)
            self.assertTrue(p["protection_id"])
        self.assertIsNone(self.engine.state["halt"])
        self.api.now += 1000
        self.engine.tick(self.api.now, [self.signal("BTCUSDT"), self.signal("ETHUSDT")], gate())
        self.assertEqual(len(self.api.writes), 2)

    def test_one_position_setting_blocks_second_without_altering_first(self):
        self.engine = CCEngine(self.api, self.store, dict(self.config, max_positions=1),
                               lambda: True, clock=lambda: self.api.now)
        self.engine.tick(NOW, [self.signal("BTCUSDT"), self.signal("ETHUSDT")], gate())
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(set(self.engine.state["positions"]), {"BTCUSDT"})

    def test_daily_account_guard_blocks_entry_but_structural_management_continues(self):
        p = self.open()
        day, _ = _period(NOW)
        self.engine.state["risk"]["days"][day]["net"] = -25.
        self.engine.save()
        self.api.now += 1000
        self.engine.tick(self.api.now, [self.signal("ETHUSDT")], gate(),
                         exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        entries = [body for _, body in self.api.writes if body["orderType"] == "limit"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(p["exit_reason"], "STRUCTURE_REVERSAL")
        self.assertIn("DAILY_LOSS_LIMIT", self.engine.state["risk"]["active_blocks"])

    def test_unexpected_fok_partial_halts_and_manages_actual_fill_without_topup(self):
        instrument = self.api.instrument
        self.api.instrument = lambda symbol: replace(instrument(symbol), qty_step=.002, min_qty=.002)
        self.api.partial_fraction, self.api.entry_status = .5, "cancelled"
        p = self.open()
        self.assertEqual(self.engine.state["halt"], "FOK_PARTIAL_FILL_PROTOCOL_VIOLATION")
        self.assertAlmostEqual(p["qty"], float(self.api.writes[0][1]["qty"]) / 2)
        self.assertTrue(p["protection_id"])
        self.assertNotIn("force_exit", p)
        restarted = CCEngine(self.api, self.store, self.config, lambda: True,
                             clock=lambda: self.api.now)
        restarted.tick(self.api.now, [self.signal()], gate())
        self.assertEqual(len(self.api.writes), 1)
        restarted.tick(self.api.now, exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        self.assertEqual(len(self.api.writes), 2)
        self.assertEqual(float(self.api.writes[-1][1]["qty"]), p["qty"])

    def test_exact_entry_child_is_not_counted_as_native_close(self):
        p = self.open()
        close = self.close_child(p)
        self.api.native_rows[p["protection_id"]] = [self.entry_child(p), close]
        self.api.now = NOW + 2000
        self.engine.tick(self.api.now)
        self.assertIsNone(self.engine.state["halt"])
        self.assertEqual(p["closed_qty"], 0.)
        self.assertEqual(p["qty"], p["original_qty"])
        self.assertTrue(p["native_child_active"])
        self.assertNotIn(p["entry_oid"], p["native_children"])
        self.assertIn(close["subOrderId"], p["native_children"])
        self.assertEqual(len(self.api.writes), 1)

    def test_unrelated_open_child_is_never_ignored(self):
        p = self.open()
        wrong = self.entry_child(p)
        wrong["subOrderId"] = "another-opening-order"
        self.api.native_rows[p["protection_id"]] = [wrong]
        self.api.now += 1000
        self.engine.tick(self.api.now)
        self.assertEqual(self.engine.state["halt"], "NATIVE_CHILD_OWNERSHIP_MISMATCH")
        self.assertEqual(p["closed_qty"], 0.)
        self.assertEqual(len(self.api.writes), 1)

    def test_entry_child_alternate_client_id_requires_canonical_order_proof(self):
        p = self.open()
        child = self.entry_child(p)
        child["subClientOid"] = "exchange-generated-client-id"
        self.api.native_rows[p["protection_id"]] = [child]
        self.api.now += 1000
        self.engine.tick(self.api.now)
        self.assertIsNone(self.engine.state["halt"])
        self.assertEqual(p["closed_qty"], 0.)
        self.assertNotIn(p["entry_oid"], p.get("native_children", {}))
        self.assertEqual(len(self.api.writes), 1)

    def test_same_entry_id_with_failed_canonical_client_proof_is_not_ignored(self):
        p = self.open()
        wrong = self.entry_child(p)
        wrong["subClientOid"] = "other-client-id"
        self.api.order_rows[p["entry_cid"]]["clientOid"] = "not-the-owned-client-id"
        self.api.native_rows[p["protection_id"]] = [wrong]
        self.api.now += 1000
        self.engine.tick(self.api.now)
        self.assertTrue(self.engine.state["halt"])
        self.assertEqual(p["closed_qty"], 0.)
        self.assertEqual(len(self.api.writes), 1)

    def test_exact_entry_id_with_changed_quantity_is_not_trusted(self):
        p = self.open()
        wrong = self.entry_child(p)
        wrong["cumExecQty"] = str(p["original_qty"] + .01)
        self.api.native_rows[p["protection_id"]] = [wrong]
        self.api.now += 1000
        self.engine.tick(self.api.now)
        self.assertTrue(self.engine.state["halt"])
        self.assertEqual(p["closed_qty"], 0.)
        self.assertEqual(len(self.api.writes), 1)

    def test_entry_child_plus_real_partial_close_accounts_only_close_and_once(self):
        p = self.open()
        qty = .01
        close = self.close_child(p, "partially_filled", qty)
        self.api.native_rows[p["protection_id"]] = [self.entry_child(p), close]
        self.api.fill_rows[close["subOrderId"]] = [{
            "orderId": close["subOrderId"], "symbol": p["symbol"],
            "category": CAT, "side": "sell", "tradeSide": "close",
            "execId": "real-native-fill-1", "execQty": str(qty),
            "execPrice": str(p["exchange_stop"]),
            "createdTime": str(NOW + 2000),
            "feeDetail": [{"feeCoin": "USDT", "fee": "-.0388"}],
            "execPnl": "-3",
        }]
        self.api.position_rows[0]["total"] = str(p["original_qty"] - qty)
        self.api.stop_rows = []
        self.api.now = NOW + 2000
        self.engine.tick(self.api.now)
        self.assertIsNone(self.engine.state["halt"])
        self.assertAlmostEqual(p["closed_qty"], qty)
        self.assertAlmostEqual(p["qty"], p["original_qty"] - qty)
        restarted = CCEngine(self.api, self.store, self.config, lambda: True,
                             clock=lambda: self.api.now)
        restarted.tick(self.api.now + 1000)
        self.assertAlmostEqual(restarted.state["positions"]["BTCUSDT"]["closed_qty"], qty)
        self.assertEqual(len(self.api.writes), 1)

    def test_verified_flat_settles_stale_live_child_once_without_any_close_write(self):
        p = self.open()
        self.engine.halt("NATIVE_CHILD_OWNERSHIP_MISMATCH")
        self.api.native_rows[p["protection_id"]] = [self.entry_child(p), self.close_child(p)]
        self.api.position_rows, self.api.stop_rows = [], []
        self.api.now = NOW + 20000
        self.api.history = self.history(p)
        self.engine.tick(self.api.now)
        self.assertEqual(self.engine.state["positions"], {})
        self.assertEqual(len(self.api.writes), 1)
        trade = self.store.trades()[0]
        self.assertEqual(trade["qty"], 0.)
        self.assertEqual(trade["closed_qty"], p["original_qty"])
        self.assertEqual(trade["net_usdt"], -9.5715268)
        self.assertEqual(trade["accounting"], "EXCHANGE_POSITION_HISTORY_VERIFIED")
        restarted = CCEngine(self.api, self.store, self.config, lambda: True,
                             clock=lambda: self.api.now)
        restarted.tick(self.api.now + 1000, [self.signal("ETHUSDT")], gate())
        self.assertEqual(len(self.store.trades()), 1)
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(restarted.state["risk"]["consecutive_losses"], 1)
        self.assertEqual(restarted.state["halt"], "NATIVE_CHILD_OWNERSHIP_MISMATCH")

    def test_flat_without_verified_history_retains_position_and_blocks_entries(self):
        p = self.open()
        self.api.native_rows[p["protection_id"]] = [self.entry_child(p), self.close_child(p)]
        self.api.position_rows, self.api.stop_rows = [], []
        self.api.now = NOW + 20000
        self.engine.tick(self.api.now, [self.signal("ETHUSDT")], gate())
        self.assertIn("BTCUSDT", self.engine.state["positions"])
        self.assertEqual(self.store.trades(), [])
        self.assertTrue(self.engine.state["halt"])
        self.assertEqual(len(self.api.writes), 1)

    def test_flat_with_pending_exit_does_not_finalize_or_resubmit(self):
        p = self.open()
        self.api.now += 1000
        self.engine.tick(self.api.now, exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        self.assertTrue(self.engine.state["pending"])
        self.api.order_rows = {}
        self.api.position_rows, self.api.stop_rows = [], []
        self.api.now = NOW + 20000
        self.api.history = self.history(p)
        self.engine.tick(self.api.now)
        self.assertIn("BTCUSDT", self.engine.state["positions"])
        self.assertTrue(self.engine.state["pending"])
        self.assertEqual(self.store.trades(), [])
        self.assertEqual(len(self.api.writes), 2)

    def test_unreadable_position_is_not_flat_evidence(self):
        p = self.open()
        self.api.history = self.history(p)
        def unavailable():
            raise DataError("POSITION_READ_FAILED")
        self.api.positions = unavailable
        self.api.now += 1000
        self.engine.tick(self.api.now)
        self.assertIn("BTCUSDT", self.engine.state["positions"])
        self.assertEqual(self.store.trades(), [])
        self.assertEqual(len(self.api.writes), 1)

    def test_nonlist_position_response_is_not_flat_evidence(self):
        p = self.open()
        self.api.history = self.history(p)
        self.api.positions = lambda: None
        self.api.now += 1000
        self.engine.tick(self.api.now)
        self.assertIn("BTCUSDT", self.engine.state["positions"])
        self.assertEqual(self.store.trades(), [])
        self.assertEqual(len(self.api.writes), 1)
        self.assertTrue(self.engine.state["halt"])

    def test_malformed_position_rows_are_not_flat_evidence(self):
        p = self.open()
        self.api.history = self.history(p)
        self.api.positions = lambda: [{"symbol": "BTCUSDT"}]
        self.api.now += 1000
        self.engine.tick(self.api.now)
        self.assertIn("BTCUSDT", self.engine.state["positions"])
        self.assertEqual(self.store.trades(), [])
        self.assertEqual(len(self.api.writes), 1)
        self.assertTrue(self.engine.state["halt"])


if __name__ == "__main__":
    unittest.main()
