"""Recovery must settle, preserve losses, and never grant order authority."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cci_chop_v1 import cli, config
from cci_chop_v1._compat.core import CAT, SafetyError
from cci_chop_v1._compat.store import Store
from cci_chop_v1.engine import CCEngine, NAMESPACE
from cci_chop_v1.recovery import recover_flat
from test_cci_chop_engine import FakeAPI, NOW, gate


class FlatMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = cli.db_path(self.root, "live")
        self.store = Store(self.path)
        self.api = FakeAPI(self.store)
        self.api.write = False
        self.api.get = lambda *a, **kw: {"userId": "test-uid", "permissions": ["uta_trade", "uta_mgt"]}
        engine = CCEngine(self.api, self.store, {}, lambda: True, clock=lambda: self.api.now)
        engine.tick(NOW, [{"symbol": "BTCUSDT", "side": "LONG", "stop": 9900.,
                         "entry": 10000., "signal_ts": NOW-60000}], gate())
        self.p = copy.deepcopy(engine.state["positions"]["BTCUSDT"])
        self.p["exchange_position_id"] = "closed-p"
        engine.state["positions"]["BTCUSDT"]["exchange_position_id"] = "closed-p"
        self.api.position_rows = []
        self.api.stop_rows = []
        self.api.history = {"position_id": "closed-p", "closed": NOW+5000,
                            "exit": 9900., "gross_usdt": -8.51968,
                            "fee_cashflow_usdt": -.68208108,
                            "funding_usdt": -.3697657, "net_usdt": -9.5715268,
                            "accounting": "EXCHANGE_POSITION_HISTORY_VERIFIED"}
        engine.state["halt"] = "NATIVE_CHILD_OWNERSHIP_MISMATCH"
        engine.save()
        self.store.set("cci_chop_runtime", {"running": False,
                       "binding": {"account_uid": "test-uid", "release_sha256": "old"}})
        self.store.close()
        old_config = {k: v for k, v in config.DEFAULT.items()
                      if k not in {"sizing_policy", "target_margin_usdt"}}
        (self.root / "cci_chop_config.json").write_text(json.dumps(old_config))

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        with __import__('sqlite3').connect(self.path) as db:
            return json.loads(db.execute("SELECT json FROM state WHERE key=?", (NAMESPACE,)).fetchone()[0])

    def test_verified_full_close_preserves_loss_and_requires_separate_arm(self):
        before_writes = copy.deepcopy(self.api.writes)
        result = recover_flat(self.root, self.api, clock=lambda: NOW+6000)
        state = self.state()
        self.assertEqual(state["positions"], {})
        self.assertIsNone(state["halt"])
        self.assertAlmostEqual(sum(v["net"] for v in state["risk"]["days"].values()), -9.5715268)
        self.assertEqual(config.load(self.root)["sizing_policy"], "fixed_margin")
        self.assertTrue((cli.state_dir(self.root)/"PAUSE").exists())
        self.assertFalse((cli.state_dir(self.root)/"ARM.json").exists())
        self.assertEqual(self.api.writes, before_writes)
        self.assertFalse(result["entry_enabled"])
        self.assertTrue((Path(result["backup_directory"])/"live.sqlite").exists())
        self.assertEqual(result["settled"][0]["net_usdt"], -9.5715268)
        recover_flat(self.root, self.api, clock=lambda: NOW+7000)
        self.assertAlmostEqual(sum(v["net"] for v in self.state()["risk"]["days"].values()), -9.5715268)

    def test_open_venue_position_blocks_migration(self):
        self.api.position_rows = [{"symbol": "BTCUSDT", "category": CAT, "total": ".001"}]
        with self.assertRaisesRegex(SafetyError, "ACCOUNT_NOT_FLAT"):
            recover_flat(self.root, self.api, clock=lambda: NOW+6000)
        self.assertEqual(self.state()["positions"]["BTCUSDT"]["qty"], self.p["qty"])

    def test_unfilled_order_blocks_migration_even_with_no_position(self):
        original = self.api.inventory
        def snapshot():
            data = original()
            data["orders"] = [{"orderId": "stale-child"}]
            return data
        self.api.inventory = snapshot
        with self.assertRaisesRegex(SafetyError, "ACCOUNT_NOT_FLAT"):
            recover_flat(self.root, self.api, clock=lambda: NOW+6000)
        self.assertEqual(config.load(self.root)["sizing_policy"], "risk_budget")

    def test_unknown_history_retains_record_and_halt(self):
        self.api.history = None
        with self.assertRaisesRegex(SafetyError, "ACCOUNTING_UNRESOLVED"):
            recover_flat(self.root, self.api, clock=lambda: NOW+6000)
        self.assertIn("BTCUSDT", self.state()["positions"])
        self.assertTrue((cli.state_dir(self.root)/"PAUSE").exists())
        self.assertEqual(config.load(self.root)["sizing_policy"], "risk_budget")

    def test_other_halt_cannot_be_silently_cleared(self):
        store = Store(self.path)
        state = store.get(NAMESPACE)
        state["halt"] = "FOREIGN_POSITION_PRESENT"
        store.set(NAMESPACE, state)
        store.close()
        with self.assertRaisesRegex(SafetyError, "UNRECOGNIZED_HALT"):
            recover_flat(self.root, self.api, clock=lambda: NOW+6000)

    def test_active_runner_lock_blocks_migration(self):
        with cli.single_runner(self.root):
            with self.assertRaisesRegex(SafetyError, "ANOTHER_CC_RUNNER"):
                recover_flat(self.root, self.api, clock=lambda: NOW+6000)

    def test_snapshot_staleness_never_claims_current_holdings(self):
        s = {"positions": {"BTCUSDT": {"position_verification": {
             "status": "EXCHANGE_FLAT_ACCOUNTING_PENDING"}}},
             "exchange_positions": {"checked_at": 100000, "positions": []}}
        fresh = cli.position_observation(s, at=100001)
        stale = cli.position_observation(s, at=131000)
        self.assertEqual(fresh["venue_position_count"], 0)
        self.assertEqual(fresh["accounting_pending_count"], 1)
        self.assertIsNone(stale["venue_position_count"])

    def test_isolated_configuration_does_not_override_crossed_5x(self):
        rows = [{"category": CAT, "symbol": symbol, "marginMode": mode,
                 "leverage": str(leverage)}
                for symbol in ("BTCUSDT", "ETHUSDT")
                for mode, leverage in (("isolated", 3), ("crossed", 5))]
        cli.validate_account_settings({"accountMode": "unified", "holdMode": "hedge_mode",
                                      "symbolConfigList": rows}, config.DEFAULT)


if __name__ == "__main__":
    unittest.main()
