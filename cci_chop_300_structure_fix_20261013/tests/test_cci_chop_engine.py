import copy
import tempfile
import unittest
from pathlib import Path

from cci_chop_v1._compat.core import CAT, Instrument, Quote, Rejected, SafetyError, UnknownOrder
from cci_chop_v1._compat.store import Store
from cci_chop_v1.engine import NAMESPACE, CCEngine

NOW = 1791158400000


def gate(side="LONG", **overrides):
    return {"eligible": True, "side": side, "model_sha256": "a" * 64,
            "paper_only": False, "demo_only": False, "deployment_approved": True,
            "native_execution_verified": True, "prospective_verified": True, **overrides}


def experimental_gate(**overrides):
    return gate(authorization_kind="USER_DIRECTED_EXPERIMENTAL",
                unvalidated_live_acknowledged=True, deployment_approved=False,
                native_execution_verified=False, prospective_verified=False, **overrides)


class FakeAPI:
    def __init__(self, store):
        self.store = store
        self.now = NOW
        self.writes = []
        self.order_rows = {}
        self.fill_rows = {}
        self.position_rows = []
        self.stop_rows = []
        self.mark = 10000.
        self.unknown_entry = False
        self.reject_exit = False
        self.modify_unknown = False
        self.modify_apply = True
        self.history = None
        self.demo = False
        self.hold_mode = "one_way_mode"
        self.partial_fraction = 1.
        self.entry_status = "filled"
        self.attach_stop = True
        self.native_rows = {}
        self.native_error = None

    def inventory(self):
        native_open = [{"orderId": row["subOrderId"], "clientOid": row["subClientOid"]}
                       for rows in self.native_rows.values() for row in rows
                       if row["status"] in ("new", "live", "partially_filled")]
        return {"positions": copy.deepcopy(self.position_rows), "orders": native_open,
                "strategies": copy.deepcopy(self.stop_rows), "equity": 1042.19,
                "assets": {"usdtEquity": "1042.19", "effEquity": "1042.19", "imr": "0", "assets": [
                    {"coin": "USDT", "equity": "1042.19", "usdValue": "1042.19", "balance": "1042.19",
                     "available": "1042.19", "debt": "0", "locked": "0"}]},
                "settings": {"accountMode": "unified", "holdMode": self.hold_mode, "symbolConfigList": [
                    {"category": CAT, "symbol": s, "marginMode": "crossed", "leverage": "5"}
                    for s in ("BTCUSDT", "ETHUSDT")]}}

    def instrument(self, symbol):
        return Instrument(symbol, .1, .001, .001, 5., 1000., .0006)

    def quote(self, symbol):
        return Quote(symbol, self.now, self.mark, self.mark - .5, self.mark + .5, self.mark, self.mark, .0001)

    def fee(self, symbol):
        return .0004

    def positions(self):
        return copy.deepcopy(self.position_rows)

    def strategies(self, kind="tpsl"):
        return copy.deepcopy(self.stop_rows)

    def strategy_sub_orders(self, parent_id):
        if self.native_error:
            raise self.native_error
        return copy.deepcopy(self.native_rows.get(parent_id, []))

    def order(self, oid, cid):
        if cid not in self.order_rows:
            from cci_chop_v1._compat.core import DataError
            raise DataError("not propagated")
        return copy.deepcopy(self.order_rows[cid])

    def fills(self, oid):
        return copy.deepcopy(self.fill_rows.get(oid, []))

    def history_match(self, position, now):
        return copy.deepcopy(self.history)

    def post(self, path, body):
        state = self.store.get(NAMESPACE)
        if path.endswith("place-order"):
            assert body["clientOid"] in state["pending"], "intent must precede mutation"
        if path.endswith("modify-strategy-order"):
            assert body["symbol"] in state["modifications"], "modify intent must precede mutation"
        self.writes.append((path, copy.deepcopy(body)))
        if path.endswith("cancel-order"):
            return {}
        if path.endswith("modify-strategy-order"):
            if self.modify_apply:
                for row in self.stop_rows:
                    if row["orderId"] == body["orderId"]:
                        row["stopLoss"] = body["stopLoss"]
                        row["qty"] = body["qty"]
            if self.modify_unknown:
                raise UnknownOrder("mod response lost")
            return {"orderId": body["orderId"]}
        entry = body["orderType"] == "limit"
        if entry and self.unknown_entry:
            raise UnknownOrder("entry response lost")
        if not entry and self.reject_exit:
            raise Rejected("exit rejected")
        oid = "order-" + str(len(self.writes))
        qty = float(body["qty"]) * (self.partial_fraction if entry else 1.)
        side = body.get("posSide") or ("long" if body["side"] == "buy" else "short")
        if not entry:
            side = body.get("posSide") or ("long" if body["side"] == "sell" else "short")
        self.order_rows[body["clientOid"]] = {"orderId": oid, "clientOid": body["clientOid"], "symbol": body["symbol"],
            "category": CAT, "side": body["side"], "posSide": side,
            "orderStatus": self.entry_status if entry else "filled", "cumExecQty": str(qty)}
        self.fill_rows[oid] = [{"orderId": oid, "symbol": body["symbol"], "side": body["side"], "execId": oid + "-fill",
            "execQty": str(qty), "execPrice": str(self.mark), "createdTime": str(self.now),
            "feeDetail": [{"feeCoin": "USDT", "fee": str(-qty * self.mark * .0004)}], "execPnl": "0"}]
        if entry:
            self.position_rows.append({"positionId": "p-" + oid, "symbol": body["symbol"], "category": CAT,
                "posSide": side, "total": str(qty), "createdTime": str(self.now), "marginMode": "crossed", "leverage": "5"})
            if self.attach_stop:
                self.stop_rows.append({"orderId": "s-" + oid, "symbol": body["symbol"], "category": CAT, "posSide": side,
                    "qty": str(qty), "stopLoss": body["stopLoss"], "createdTime": str(self.now), "status": "pending",
                    "slTriggerBy": "mark", "slOrderType": "market"})
        else:
            self.position_rows = [r for r in self.position_rows if r["symbol"] != body["symbol"]]
            self.stop_rows = [r for r in self.stop_rows if r["symbol"] != body["symbol"]]
        return {"orderId": oid}


class CCEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite")
        self.api = FakeAPI(self.store)
        self.authorized = True
        self.engine = CCEngine(self.api, self.store, {"symbols": ["BTCUSDT", "ETHUSDT"]}, lambda: self.authorized, clock=lambda: self.api.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def signal(self, symbol="BTCUSDT", side="LONG", **extra):
        return {"symbol": symbol, "side": side, "stop": 9900. if side == "LONG" else 10100., "entry": 10000.,
                "signal_ts": NOW - 60000, "signal_id": symbol + side + str(NOW), **extra}

    def open(self, side="LONG", symbol="BTCUSDT"):
        self.engine.tick(NOW, [self.signal(symbol, side)], {symbol: gate(side)})
        self.api.now = NOW + 1000
        self.engine.tick(self.api.now)
        return self.engine.state["positions"][symbol]

    def test_entries_require_promoted_native_gate_and_authorization(self):
        for g in (gate(eligible=False), gate(native_execution_verified=False), gate(paper_only=True), gate(demo_only=True), gate(side="SHORT")):
            self.engine.tick(NOW, [self.signal()], g)
        self.authorized = False
        self.engine.tick(NOW, [self.signal()], gate())
        self.assertEqual(self.api.writes, [])

    def test_explicit_experimental_ack_and_injected_config_can_enter_without_faking_proof(self):
        self.engine = CCEngine(self.api, self.store, {"experimental_live": True},
                               lambda: self.authorized, clock=lambda: self.api.now)
        g = experimental_gate()
        self.engine.tick(NOW, [self.signal()], g)
        self.assertEqual(len(self.api.writes), 1)
        self.assertFalse(g["deployment_approved"])
        self.assertFalse(g["native_execution_verified"])
        self.assertFalse(g["prospective_verified"])
        self.assertFalse(self.engine.state["operational_execution_verified"])

    def test_experimental_live_requires_ack_config_and_authorized_callback(self):
        self.engine.tick(NOW, [self.signal()], experimental_gate())
        enabled = CCEngine(self.api, self.store, {"experimental_live": True},
                           lambda: self.authorized, clock=lambda: self.api.now)
        denied = [dict(experimental_gate(), unvalidated_live_acknowledged=False),
                  dict(experimental_gate(), deployment_approved=True),
                  dict(experimental_gate(), native_execution_verified=True),
                  dict(experimental_gate(), prospective_verified=True),
                  dict(experimental_gate(), authorization_kind="UNKNOWN")]
        for g in denied:
            enabled.tick(NOW, [self.signal()], g)
        self.authorized = False
        enabled.tick(NOW, [self.signal()], experimental_gate())
        self.assertEqual(self.api.writes, [])

    def native_partial(self, p, status="partially_filled"):
        child_id = "native-child-1"
        qty = p["original_qty"] / 2
        row = {"subOrderId": child_id, "subClientOid": "native-client-1",
               "category": CAT, "symbol": p["symbol"], "qty": str(p["original_qty"]),
               "cumExecQty": str(qty), "side": "sell" if p["side"] == "LONG" else "buy",
               "posSide": p["side"].lower(), "status": status, "createdTime": str(NOW + 2000)}
        self.api.native_rows[p["protection_id"]] = [row]
        self.api.fill_rows[child_id] = [{"orderId": child_id, "symbol": p["symbol"],
            "category": CAT, "side": row["side"], "tradeSide": "close", "execId": "native-fill-1",
            "execQty": str(qty), "execPrice": str(p["exchange_stop"]), "createdTime": str(NOW + 2000),
            "feeDetail": [{"feeCoin": "USDT", "fee": str(-qty * p["exchange_stop"] * .0004)}], "execPnl": "-1"}]
        self.api.position_rows[0]["total"] = str(p["original_qty"] - qty)
        self.api.stop_rows = []
        self.api.now = NOW + 2000
        return row

    def test_native_active_partial_fill_is_owned_and_defers_close_and_new_entry(self):
        p = self.open()
        original_qty = p["qty"]
        self.native_partial(p)
        self.engine.tick(self.api.now, [self.signal("ETHUSDT")], gate(),
                         exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        self.assertIsNone(self.engine.state["halt"])
        self.assertTrue(p["native_child_active"])
        self.assertAlmostEqual(p["qty"], original_qty / 2)
        self.assertEqual(len(self.api.writes), 1)
        self.assertTrue(p["native_children"]["native-child-1"]["fills"])

    def test_terminal_native_partial_closes_only_fresh_verified_residual(self):
        p = self.open()
        original_qty = p["qty"]
        self.native_partial(p, "cancelled")
        self.engine.tick(self.api.now)
        self.assertEqual(len(self.api.writes), 2)
        body = self.api.writes[-1][1]
        self.assertEqual(body["reduceOnly"], "yes")
        self.assertAlmostEqual(float(body["qty"]), original_qty / 2)
        self.assertEqual(p["exit_reason"], "NATIVE_STOP_TERMINAL_RESIDUAL")
        self.assertFalse(p["native_child_active"])

    def test_native_partial_restart_repeated_reads_do_not_double_count_or_resend(self):
        p = self.open()
        original_qty = p["qty"]
        native = self.native_partial(p)
        self.engine.tick(self.api.now)
        restarted = CCEngine(self.api, self.store, {}, lambda: True, clock=lambda: self.api.now)
        restarted.tick(self.api.now + 1000, exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        managed = restarted.state["positions"]["BTCUSDT"]
        self.assertAlmostEqual(managed["closed_qty"], original_qty / 2)
        self.assertEqual(len(self.api.writes), 1)
        native["status"] = "cancelled"
        self.api.now += 2000
        restarted.tick(self.api.now)
        self.assertAlmostEqual(managed["closed_qty"], original_qty / 2)
        self.assertEqual(len(self.api.writes), 2)
        again = CCEngine(self.api, self.store, {}, lambda: True, clock=lambda: self.api.now)
        self.api.now += 1000
        again.tick(self.api.now)
        self.assertEqual(len(self.api.writes), 2)
        self.assertAlmostEqual(again.state["positions"]["BTCUSDT"]["closed_qty"], original_qty)

    def test_native_missing_fills_halts_without_adopting_exchange_quantity(self):
        p = self.open()
        original_qty = p["qty"]
        self.native_partial(p, "cancelled")
        self.api.fill_rows["native-child-1"] = []
        self.engine.tick(self.api.now)
        self.assertAlmostEqual(p["qty"], original_qty)
        self.assertTrue(p["native_children_unresolved"])
        self.assertIn("FILLS_NOT_YET", self.engine.state["halt"])
        self.assertEqual(len(self.api.writes), 1)

    def test_native_child_fill_wrong_side_cannot_authorize_residual_close(self):
        p = self.open()
        original_qty = p["qty"]
        self.native_partial(p, "cancelled")
        self.api.fill_rows["native-child-1"][0]["side"] = "buy"
        self.engine.tick(self.api.now)
        self.assertAlmostEqual(p["qty"], original_qty)
        self.assertEqual(self.engine.state["halt"], "NATIVE_CHILD_FILL_OWNERSHIP_MISMATCH")
        self.assertEqual(len(self.api.writes), 1)

    def test_native_unknown_status_and_read_failure_never_send_another_close(self):
        from cci_chop_v1._compat.core import DataError
        p = self.open()
        self.native_partial(p, "unknown")
        self.engine.tick(self.api.now, exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        self.assertEqual(self.engine.state["halt"], "NATIVE_CHILD_STATUS_UNKNOWN")
        self.assertEqual(len(self.api.writes), 1)
        self.api.native_error = DataError("native read failed")
        self.api.now += 1000
        self.engine.tick(self.api.now, exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        self.assertEqual(len(self.api.writes), 1)

    def test_native_cumulative_regression_is_not_accounted(self):
        p = self.open()
        native = self.native_partial(p)
        self.engine.tick(self.api.now)
        original_closed = p["closed_qty"]
        native["cumExecQty"] = "0"
        self.api.now += 1000
        self.engine.tick(self.api.now)
        self.assertEqual(self.engine.state["halt"], "NATIVE_CHILD_CUMULATIVE_FILL_REGRESSED")
        self.assertEqual(p["closed_qty"], original_closed)
        self.assertEqual(len(self.api.writes), 1)

    def test_native_active_to_fully_filled_finishes_without_extra_exit_and_counts_once(self):
        p = self.open()
        original_qty = p["qty"]
        native = self.native_partial(p)
        self.engine.tick(self.api.now)
        native.update(status="filled", cumExecQty=str(original_qty))
        first = self.api.fill_rows["native-child-1"][0]
        self.api.fill_rows["native-child-1"].append({**first, "execId": "native-fill-2",
                                                   "createdTime": str(NOW + 3000)})
        self.api.position_rows = []
        self.api.now = NOW + 3000
        self.api.history = {"accounting": "EXCHANGE_POSITION_HISTORY_VERIFIED", "net_usdt": -1.2,
                            "closed": self.api.now, "funding_usdt": 0., "exit": 9900.}
        self.engine.tick(self.api.now)
        self.engine.tick(self.api.now + 1000)
        self.assertEqual(self.engine.state["positions"], {})
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(len(self.store.trades()), 1)
        self.assertAlmostEqual(self.store.trades()[0]["closed_qty"], original_qty)

    def test_native_residual_order_quantity_has_no_binary_float_tail(self):
        p = self.open()
        native = self.native_partial(p, "cancelled")
        native["cumExecQty"] = "0.01"
        self.api.fill_rows["native-child-1"][0]["execQty"] = "0.01"
        self.api.position_rows[0]["total"] = str(p["original_qty"] - .01)
        self.engine.tick(self.api.now)
        self.assertEqual(self.api.writes[-1][1]["qty"], "0.072")

    def test_paper_gate_never_live_and_demo_requires_demo_adapter(self):
        self.engine.tick(NOW, [self.signal()], gate(paper_only=True, demo_only=False))
        demo = CCEngine(self.api, self.store, {"mode": "demo"}, lambda: True, clock=lambda: self.api.now)
        demo.tick(NOW, [self.signal()], gate(demo_only=True))
        self.assertEqual(self.api.writes, [])

    def test_dynamic_risk_shrinks_margin_and_preset_guard(self):
        p = self.open()
        body = self.api.writes[0][1]
        self.assertEqual(body["timeInForce"], "ioc")
        self.assertEqual(body["slTriggerBy"], "mark")
        self.assertLess(p["entry"] * p["qty"] / 5, 300)
        self.assertLessEqual(p["initial_risk_usdt"], 10.4219)
        self.assertTrue(p["protection_id"])

    def test_ambiguous_entry_is_not_replayed_after_restart(self):
        self.api.unknown_entry = True
        self.engine.tick(NOW, [self.signal()], gate())
        restarted = CCEngine(self.api, self.store, {}, lambda: True, clock=lambda: self.api.now)
        restarted.tick(NOW + 1000, [self.signal()], gate())
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(len(restarted.state["pending"]), 1)

    def test_replayed_signal_never_reenters(self):
        self.open()
        self.engine.tick(NOW + 1000, [self.signal()], gate())
        self.assertEqual(sum(path.endswith("place-order") for path, _ in self.api.writes), 1)

    def test_expired_signal_and_price_chase_do_not_write(self):
        self.engine.tick(NOW + 90001, [self.signal()], gate())
        self.api.mark = 10050
        self.engine.tick(NOW, [self.signal()], gate())
        self.assertEqual(self.api.writes, [])

    def test_foreign_order_halts_without_cancel_or_adoption(self):
        snapshot = self.api.inventory
        def inventory():
            data = snapshot()
            data["orders"] = [{"clientOid": "foreign", "orderId": "foreign"}]
            return data
        self.api.inventory = inventory
        self.engine.tick(NOW, [self.signal()], gate())
        self.assertEqual(self.engine.state["halt"], "FOREIGN_ORDER_PRESENT")
        self.assertEqual(self.api.writes, [])

    def test_opposite_hedge_side_is_foreign_even_same_symbol_quantity_time(self):
        p = self.open()
        foreign = copy.deepcopy(self.api.position_rows[0])
        foreign["posSide"] = "short"
        self.api.position_rows.append(foreign)
        self.engine.tick(NOW + 2000)
        self.assertIn("POSITION_SIDE", self.engine.state["halt"])
        self.assertEqual(len(self.api.writes), 1)

    def test_external_partial_position_change_halts_and_is_not_closed(self):
        self.open()
        self.api.position_rows[0]["total"] = "0.001"
        self.engine.tick(NOW + 2000)
        self.assertEqual(self.engine.state["halt"], "POSITION_QUANTITY_CHANGED")
        self.assertEqual(len(self.api.writes), 1)

    def test_missing_native_guard_emergency_close_even_paused(self):
        self.api.attach_stop = False
        self.open()
        self.authorized = False
        self.api.now = NOW + 16000
        self.engine.tick(self.api.now)
        self.assertEqual(self.engine.state["halt"], "NATIVE_STOP_MISSING_OR_CHANGED")
        self.assertEqual(self.api.writes[-1][1]["reduceOnly"], "yes")

    def test_stop_tightens_only_after_readback_and_never_loosen(self):
        p = self.open()
        self.api.now = NOW + 2000
        self.engine.tick(self.api.now, dynamic_stops={"BTCUSDT": 9950})
        self.assertEqual(p["exchange_stop"], 9900)
        self.assertEqual(self.api.writes[-1][1]["qty"], str(p["qty"]))
        self.engine.tick(NOW + 3000)
        self.assertEqual(p["exchange_stop"], 9950)
        writes = len(self.api.writes)
        self.engine.tick(NOW + 4000, dynamic_stops={"BTCUSDT": 9940})
        self.assertEqual(len(self.api.writes), writes)

    def test_unknown_stop_update_preserves_old_guard_no_resend(self):
        p = self.open()
        self.api.modify_unknown, self.api.modify_apply = True, False
        self.api.now = NOW + 2000
        self.engine.tick(self.api.now, dynamic_stops={"BTCUSDT": 9950})
        restarted = CCEngine(self.api, self.store, {}, lambda: True, clock=lambda: self.api.now)
        restarted.tick(NOW + 14000, dynamic_stops={"BTCUSDT": 9960})
        self.assertEqual(restarted.state["positions"]["BTCUSDT"]["exchange_stop"], 9900)
        self.assertEqual(len(self.api.writes), 2)
        self.assertIn("UNKNOWN", restarted.state["halt"])

    def test_terminal_partial_entry_manages_actual_fill_quantity(self):
        self.api.partial_fraction, self.api.entry_status = .5, "cancelled"
        p = self.open()
        self.assertAlmostEqual(p["qty"], float(self.api.writes[0][1]["qty"]) / 2)
        self.assertEqual(self.engine.state["pending"], {})
        self.assertTrue(p["protection_id"])

    def test_unprotected_nonterminal_partial_entry_cancel_once_close_owned_qty(self):
        self.api.partial_fraction, self.api.entry_status, self.api.attach_stop = .5, "partially_filled", False
        p = self.open()
        self.api.now = NOW + 16000
        self.engine.tick(self.api.now)
        self.assertEqual(sum(path.endswith("cancel-order") for path, _ in self.api.writes), 1)
        self.assertAlmostEqual(float(self.api.writes[-1][1]["qty"]), p["qty"])
        self.engine.tick(NOW + 17000)
        self.assertEqual(sum(path.endswith("cancel-order") for path, _ in self.api.writes), 1)

    def test_hedge_close_is_reverse_side_with_original_position_side(self):
        self.api.hold_mode = "hedge_mode"
        self.open(side="SHORT")
        self.engine.tick(NOW + 2000, exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        body = self.api.writes[-1][1]
        self.assertEqual((body["side"], body["posSide"]), ("buy", "short"))
        self.assertNotIn("reduceOnly", body)

    def test_exit_rejected_not_automatically_resent_after_restart(self):
        self.open()
        self.api.reject_exit = True
        self.engine.tick(NOW + 2000, exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        restarted = CCEngine(self.api, self.store, {}, lambda: True, clock=lambda: self.api.now)
        restarted.tick(NOW + 3000)
        self.assertEqual(len(self.api.writes), 2)
        self.assertTrue(restarted.state["positions"]["BTCUSDT"]["exit_rejected"])

    def test_history_only_accounting_atomic_and_namespace_isolated(self):
        self.store.set("engine", {"legacy": True})
        p = self.open()
        self.engine.tick(NOW + 2000, exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        self.api.now = NOW + 3000
        self.engine.tick(self.api.now)
        self.assertEqual(self.store.trades(), [])
        self.assertIn("BTCUSDT", self.engine.state["positions"])
        self.api.history = {"accounting": "EXCHANGE_POSITION_HISTORY_VERIFIED", "net_usdt": -3.5,
                            "closed": NOW + 3000, "funding_usdt": -.2, "exit": 9990}
        self.engine.tick(NOW + 13000)
        self.assertEqual(len(self.store.trades()), 1)
        self.assertEqual(self.store.get("engine"), {"legacy": True})
        self.assertEqual(self.engine.state["risk"]["consecutive_losses"], 1)
        self.engine.tick(NOW + 14000)
        self.assertEqual(len(self.store.trades()), 1)

    def test_daily_loss_counter_is_durable_entry_block(self):
        from cci_chop_v1.engine import _period
        day, week = _period(NOW)
        self.engine.state["risk"]["days"][day] = {"start_equity": 1042.19, "net": -25.}
        self.engine.save()
        restarted = CCEngine(self.api, self.store, {}, lambda: True, clock=lambda: self.api.now)
        restarted.tick(NOW, [self.signal()], gate())
        self.assertEqual(self.api.writes, [])
        self.assertTrue(restarted.state["halt"])

    def test_slow_account_inventory_refreshes_clock_before_fresh_quote_validation(self):
        original = self.api.inventory
        def slow_inventory():
            self.api.now += 5000
            return original()
        self.api.inventory = slow_inventory
        self.engine.tick(NOW, [self.signal()], gate())
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.engine.state["positions"]["BTCUSDT"]["opened"], NOW + 10000)
        self.engine.tick(self.api.now)
        self.assertTrue(self.engine.state["positions"]["BTCUSDT"]["protection_id"])
        self.assertIsNone(self.engine.state["halt"])

    def test_nonterminal_exit_partial_fill_is_accounted_once_without_resend(self):
        p = self.open()
        original_row = copy.deepcopy(self.api.position_rows[0])
        original_stop = copy.deepcopy(self.api.stop_rows[0])
        original_qty = p["qty"]
        self.engine.tick(NOW + 2000, exits={"BTCUSDT": "STRUCTURE_REVERSAL"})
        exit_intent = next(x for x in self.engine.state["pending"].values() if x["kind"] == "EXIT")
        detail = self.api.order_rows[exit_intent["cid"]]
        detail.update(orderStatus="partially_filled", cumExecQty=str(original_qty / 2))
        self.api.fill_rows[exit_intent["oid"]][0]["execQty"] = str(original_qty / 2)
        original_row["total"] = original_stop["qty"] = str(original_qty / 2)
        self.api.position_rows, self.api.stop_rows = [original_row], [original_stop]
        self.engine.tick(NOW + 3000)
        self.assertAlmostEqual(p["qty"], original_qty / 2)
        self.assertAlmostEqual(p["closed_qty"], original_qty / 2)
        self.engine.tick(NOW + 4000)
        self.assertAlmostEqual(p["closed_qty"], original_qty / 2)
        self.assertEqual(len(self.api.writes), 2)
        self.assertIn(exit_intent["cid"], self.engine.state["pending"])
        detail.update(orderStatus="filled", cumExecQty=str(original_qty))
        self.api.fill_rows[exit_intent["oid"]][0]["execQty"] = str(original_qty)
        self.api.position_rows, self.api.stop_rows = [], []
        self.engine.tick(NOW + 15000)
        self.assertAlmostEqual(p["closed_qty"], original_qty)
        self.assertEqual(self.engine.state["pending"], {})
        self.assertEqual(len(self.api.writes), 2)

    def test_same_bar_two_symbols_require_first_fill_and_native_guard_before_second(self):
        events = []
        self.engine.notify = events.append
        self.engine.tick(NOW, [self.signal("BTCUSDT"), self.signal("ETHUSDT")],
                         {"BTCUSDT": gate(), "ETHUSDT": gate()})
        self.assertEqual(len(self.api.writes), 2)
        self.assertEqual(set(self.engine.state["positions"]), {"BTCUSDT", "ETHUSDT"})
        self.assertIsNone(self.engine.state["halt"])
        self.assertEqual(self.engine.state["pending"], {})
        self.assertTrue(all(p.get("protection_id") for p in self.engine.state["positions"].values()))
        open_events = [e for e in events if e["kind"] == "OPEN"]
        self.assertEqual({e["symbol"] for e in open_events}, {"BTCUSDT", "ETHUSDT"})

    def test_inventory_halt_during_entry_preflight_prevents_write_and_signal_consumption(self):
        def halted_inventory(_snapshot):
            self.engine.halt("INVENTORY_REVIEW_REQUIRED")
            return True
        self.engine._owned_inventory = halted_inventory
        self.assertFalse(self.engine.enter(self.signal(), gate(), self.api.inventory(), NOW))
        self.assertEqual(self.api.writes, [])
        self.assertFalse(self.store.seen("CC_SIGNAL_" + self.signal()["signal_id"]))


if __name__ == "__main__":
    unittest.main()
