"""Offline execution semantics; test fixtures are not real Bitget evidence."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cci_chop_v1._compat.api import fill_summary
from cci_chop_v1._compat.core import CAT, Instrument, Quote, Rejected, SafetyError
from cci_chop_v1._compat.store import Store
from cci_chop_v1.paper import ACCOUNTING, GAP_REASON, PaperAdapter, STATE_KEY


class Clock:
    def __init__(self):
        self.at = 1_800_000_000_000

    def __call__(self):
        return self.at


class ReadOnlyFeed:
    write = False

    def __init__(self, clock):
        self.clock = clock
        self.mid = {"BTCUSDT": 100.0, "ETHUSDT": 100.0}
        self.personal = .0004
        self.reads = []
        self.post_calls = 0

    def instrument(self, symbol):
        return Instrument(symbol, .01, .001, .001, 5., 100., .0006)

    def quote(self, symbol):
        mid = self.mid[symbol]
        return Quote(symbol, self.clock.at, mid, mid-.01, mid+.01, mid, mid, .0001)

    def fee(self, symbol):
        self.reads.append(("fee", symbol))
        if self.personal is None:
            raise SafetyError("fee read unavailable")
        return self.personal

    def candles(self, symbol, interval, count):
        self.reads.append(("candles", symbol, interval, count))
        return []

    def settled_funding(self, symbol, now=None):
        return {"symbol": symbol, "timestamp": self.clock.at, "rate": .0001}

    def get(self, path, params=None, private=False):
        self.reads.append(("get", path, private))
        return {"fixture": True}

    def post(self, path, body):
        self.post_calls += 1
        raise AssertionError("the real exchange must never receive a POST")


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.feed = ReadOnlyFeed(self.clock)
        self.path = Path(self.temp.name)/"state.sqlite"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.paper = PaperAdapter(self.feed, self.store, now=self.clock)

    def entry(self, symbol="BTCUSDT", side="buy", cid="entry-one", price=None):
        return {"category": CAT, "symbol": symbol, "side": side, "qty": "1.000",
            "orderType": "limit", "timeInForce": "ioc", "price": str(price if price is not None else 100.10 if side == "buy" else 99.90),
            "clientOid": cid, "reduceOnly": "no", "stopLoss": "98" if side == "buy" else "102",
            "slTriggerBy": "mark", "slOrderType": "market"}

    def reduce(self, cid="exit-one", qty="1.000", symbol="BTCUSDT", side="sell"):
        return {"category": CAT, "symbol": symbol, "side": side, "qty": qty,
                "orderType": "market", "clientOid": cid, "reduceOnly": "yes"}

    def place(self, body):
        return self.paper.post("/api/v3/trade/place-order", body)

    def position_probe(self, symbol="BTCUSDT"):
        row = self.paper.positions()[0]
        return {"symbol": symbol, "side": row["posSide"].upper(), "opened": int(row["createdTime"]),
                "entry": row["openPriceAvg"], "original_qty": row["original_qty"],
                "qty_step": .001, "price_step": .01, "entry_oid": row["entry_oid"]}

    def test_durable_entry_replay_protection_and_no_exchange_post(self):
        body = self.entry()
        response = self.place(body)
        order = self.paper.order(cid=body["clientOid"])
        self.assertEqual(order["orderStatus"], "filled")
        self.assertAlmostEqual(float(self.paper.fills(response["orderId"])[0]["execPrice"]), 100.01*1.0007)
        stop = self.paper.strategies()[0]
        self.assertEqual(stop["entry_order_id"], response["orderId"])
        self.assertEqual(stop["slTriggerBy"], "mark")
        self.assertTrue(stop["paper_only"])
        reopened = PaperAdapter(self.feed, self.store, now=self.clock)
        self.assertEqual(reopened.post("/api/v3/trade/place-order", body), response)
        self.assertEqual(len(reopened.positions()), 1)
        self.assertEqual(len(reopened.state["orders"]), 1)
        self.assertEqual(self.feed.post_calls, 0)
        with self.assertRaisesRegex(SafetyError, "BODY_CONFLICT"):
            reopened.post("/api/v3/trade/place-order", {**body, "qty": "2.000"})

    def test_ioc_worse_than_limit_is_cancelled_without_position(self):
        response = self.place(self.entry(price=100.02))
        self.assertEqual(self.paper.order(response["orderId"])["orderStatus"], "cancelled")
        self.assertEqual(self.paper.fills(response["orderId"]), [])
        self.assertEqual(self.paper.positions(), [])
        self.assertEqual(self.paper.strategies(), [])

    def test_native_virtual_stop_fills_at_current_adverse_quote_and_funding_unknown(self):
        self.place(self.entry())
        probe = self.position_probe()
        stop_id = self.paper.strategies()[0]["orderId"]
        self.clock.at += 3_000
        self.feed.mid["BTCUSDT"] = 97.5
        snap = self.paper.inventory()
        self.assertEqual(snap["positions"], [])
        self.assertEqual(snap["strategies"], [])
        self.assertEqual(self.paper.state["stops"][stop_id]["status"], "triggered")
        result = self.paper.history_match(probe, self.clock.at)
        self.assertAlmostEqual(result["exit"], 97.49*.9993)
        self.assertEqual(result["accounting"], ACCOUNTING)
        self.assertIsNone(result["funding_usdt"])
        self.assertFalse(result["exchange_fills_verified"])
        self.assertLess(result["net_usdt"], result["gross_usdt"])
        self.assertEqual(self.feed.post_calls, 0)

    def test_gap_freezes_scenario_preserves_position_and_creates_no_fill(self):
        self.place(self.entry())
        before = len(self.paper.state["orders"])
        probe = self.position_probe()
        self.clock.at += 15_001
        self.feed.mid["BTCUSDT"] = 90
        with self.assertRaisesRegex(SafetyError, GAP_REASON):
            self.paper.quote("BTCUSDT")
        self.assertEqual(len(self.paper.positions()), 1)
        self.assertEqual(len(self.paper.state["orders"]), before)
        self.assertEqual(len(self.paper.state["history"]), 0)
        self.assertIsNone(self.paper.history_match(probe, self.clock.at))
        restarted = PaperAdapter(self.feed, self.store, now=self.clock)
        self.assertFalse(restarted.scenario_status()["valid"])
        self.assertTrue(restarted.positions()[0]["observation_incomplete"])
        with self.assertRaisesRegex(SafetyError, GAP_REASON):
            restarted.post("/api/v3/trade/place-order", self.reduce())

    def test_stop_tightening_readback_preserves_id_and_rejects_loosening(self):
        self.place(self.entry())
        stop_id = self.paper.strategies()[0]["orderId"]
        body = {"category": CAT, "symbol": "BTCUSDT", "orderId": stop_id,
                "qty": "1.000", "stopLoss": "99", "slTriggerBy": "mark", "slOrderType": "market"}
        self.paper.post("/api/v3/trade/modify-strategy-order", body)
        self.assertEqual(self.paper.strategies()[0]["orderId"], stop_id)
        self.assertEqual(self.paper.strategies()[0]["stopLoss"], "99.0")
        with self.assertRaisesRegex(Rejected, "CANNOT_LOOSEN"):
            self.paper.post("/api/v3/trade/modify-strategy-order", {**body, "stopLoss": "97"})
        with self.assertRaisesRegex(Rejected, "OWNERSHIP"):
            self.paper.post("/api/v3/trade/modify-strategy-order", {**body, "orderId": "foreign"})

    def test_short_stop_direction_and_adverse_cover_price(self):
        self.place(self.entry(side="sell"))
        probe = self.position_probe()
        stop_id = self.paper.strategies()[0]["orderId"]
        body = {"category": CAT, "symbol": "BTCUSDT", "orderId": stop_id,
                "qty": "1.000", "stopLoss": "101", "slTriggerBy": "mark", "slOrderType": "market"}
        self.paper.post("/api/v3/trade/modify-strategy-order", body)
        with self.assertRaisesRegex(Rejected, "CANNOT_LOOSEN"):
            self.paper.post("/api/v3/trade/modify-strategy-order", {**body, "stopLoss": "103"})
        self.clock.at += 3_000
        self.feed.mid["BTCUSDT"] = 101.5
        self.paper.quote("BTCUSDT")
        result = self.paper.history_match(probe, self.clock.at)
        self.assertAlmostEqual(result["exit"], 101.51*1.0007)
        self.assertLess(result["gross_usdt"], 0)

    def test_partial_reductions_keep_owned_guard_and_account_for_all_fees(self):
        response = self.place(self.entry())
        probe = self.position_probe()
        entry_summary = fill_summary(self.paper.fills(response["orderId"]))
        self.clock.at += 3_000
        self.feed.mid["BTCUSDT"] = 101
        first = self.place(self.reduce(cid="exit-half-one", qty="0.500"))
        self.assertEqual(self.paper.positions()[0]["total"], "0.5")
        self.assertEqual(self.paper.strategies()[0]["qty"], "0.5")
        self.assertIsNone(self.paper.history_match(probe, self.clock.at))
        second = self.place(self.reduce(cid="exit-half-two", qty="0.500"))
        result = self.paper.history_match(probe, self.clock.at)
        fees = entry_summary["fees"] + fill_summary(self.paper.fills(first["orderId"]))["fees"] + fill_summary(self.paper.fills(second["orderId"]))["fees"]
        self.assertAlmostEqual(result["fee_cashflow_usdt"], fees)
        self.assertAlmostEqual(result["net_usdt"], result["gross_usdt"]+fees)
        self.assertEqual(self.paper.positions(), [])
        self.assertEqual(self.paper.strategies(), [])

    def test_fee_fallback_is_explicit_and_private_account_never_adopted(self):
        self.feed.personal = None
        self.assertEqual(self.paper.fee("BTCUSDT"), .0006)
        observation = self.store.get(STATE_KEY)["fees"]["BTCUSDT"]
        self.assertEqual(observation["source"], "PUBLIC_CONTRACT_TAKER_FEE_FALLBACK")
        info = self.paper.get("/api/v3/account/info", private=True)
        self.assertTrue(info["userId"].startswith("PAPER_CC_"))
        self.assertFalse(any(read[0] == "get" for read in self.feed.reads))
        self.paper.get("/api/v3/market/tickers")
        self.assertEqual(self.feed.reads[-1], ("get", "/api/v3/market/tickers", False))
        with self.assertRaisesRegex(SafetyError, "PRIVATE_READ_NOT_ALLOWED"):
            self.paper.get("/api/v3/position/history-position", private=True)
        with self.assertRaisesRegex(SafetyError, "MUTATION_ENDPOINT_NOT_ALLOWED"):
            self.paper.post("/api/v3/account/set-leverage", {"category": CAT})
        self.assertEqual(self.feed.post_calls, 0)

    def test_refuses_write_enabled_source_or_changed_existing_scenario(self):
        self.feed.write = True
        with self.assertRaisesRegex(SafetyError, "READ_ONLY_SOURCE"):
            PaperAdapter(self.feed, self.store, now=self.clock)
        self.feed.write = False
        with self.assertRaisesRegex(SafetyError, "CONFIG_MISMATCH"):
            PaperAdapter(self.feed, self.store, initial_equity=2000, now=self.clock)

    def test_two_symbols_have_independent_owned_stops_and_per_symbol_cap(self):
        self.place(self.entry())
        self.place(self.entry(symbol="ETHUSDT", side="sell", cid="entry-eth"))
        self.assertEqual(len(self.paper.positions()), 2)
        self.assertEqual(len(self.paper.strategies()), 2)
        self.clock.at += 3_000
        self.feed.mid["BTCUSDT"] = 97.5
        self.paper.inventory()
        self.assertEqual([p["symbol"] for p in self.paper.positions()], ["ETHUSDT"])
        self.assertEqual([p["symbol"] for p in self.paper.strategies()], ["ETHUSDT"])
        with self.assertRaisesRegex(Rejected, "NOTIONAL_CAP"):
            self.place({**self.entry(cid="over-cap"), "qty": "20.000", "stopLoss": "90"})
        self.assertEqual(len(self.paper.positions()), 1)

    def test_engine_shadow_reconciles_entry_guard_and_virtual_native_close(self):
        from cci_chop_v1.config import DEFAULT
        from cci_chop_v1.engine import CCEngine
        c = {**DEFAULT, "mode": "shadow"}
        engine = CCEngine(self.paper, self.store, c, authorized=lambda: True, clock=self.clock)
        signal = {"symbol": "BTCUSDT", "side": "LONG", "stop": 98.,
                  "entry": 100.01, "signal_ts": self.clock.at-60_000, "signal_id": "fixture"}
        gate = {"eligible": True, "paper_only": True, "side": "LONG", "model_sha256": "0"*64}
        engine.tick(self.clock.at, signals=[signal], probability=gate)
        self.assertEqual(len(engine.state["pending"]), 0)
        self.assertTrue(engine.state["positions"]["BTCUSDT"].get("protection_id"))
        self.clock.at += 3_000
        engine.tick(self.clock.at, dynamic_stops={"BTCUSDT": 99.})
        self.assertIsNone(engine.state["halt"])
        self.assertTrue(engine.state["positions"]["BTCUSDT"]["protection_id"].startswith("PAPER_"))
        self.clock.at += 3_000
        engine.tick(self.clock.at)
        self.assertEqual(engine.state["positions"]["BTCUSDT"]["exchange_stop"], 99.)
        self.clock.at += 3_000
        self.feed.mid["BTCUSDT"] = 98.5
        engine.tick(self.clock.at)
        self.assertEqual(engine.state["positions"], {})
        self.assertIsNone(engine.state["halt"])
        trade = self.store.trades()[0]
        self.assertEqual(trade["mode"], "SHADOW")
        self.assertEqual(trade["accounting"], ACCOUNTING)
        self.assertIsNone(trade["funding_usdt"])
        self.assertEqual(self.feed.post_calls, 0)

    def test_engine_gap_halts_without_completed_trade_or_stop_fill(self):
        from cci_chop_v1.config import DEFAULT
        from cci_chop_v1.engine import CCEngine
        engine = CCEngine(self.paper, self.store, {**DEFAULT, "mode": "shadow"}, authorized=lambda: True, clock=self.clock)
        signal = {"symbol": "BTCUSDT", "side": "LONG", "stop": 98.,
                  "entry": 100.01, "signal_ts": self.clock.at-60_000, "signal_id": "gap-fixture"}
        gate = {"eligible": True, "paper_only": True, "side": "LONG", "model_sha256": "0"*64}
        engine.tick(self.clock.at, signals=[signal], probability=gate)
        self.clock.at += 3_000
        engine.tick(self.clock.at)
        before = len(self.paper.state["orders"])
        self.clock.at += 15_001
        self.feed.mid["BTCUSDT"] = 90
        engine.tick(self.clock.at)
        self.assertEqual(engine.state["halt"], GAP_REASON)
        self.assertEqual(len(engine.state["positions"]), 1)
        self.assertEqual(self.store.trades(), [])
        self.assertEqual(len(self.paper.state["orders"]), before)
        self.assertFalse(self.paper.scenario_status()["valid"])


if __name__ == "__main__":
    unittest.main()
