"""Release and operational boundaries; no network access or exchange orders."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cci_chop_v1 import cli, config, strategy
from cci_chop_v1._compat.core import SafetyError
from cci_chop_v1._compat.store import Store
from cci_chop_v1.notifications import Notifier, render


class RuntimeBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.root=Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_model(self, **extra):
        path=cli.model_path(self.root)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"strategy_sha256":strategy.spec_sha256(),**extra}))

    def test_unreleased_arm_cannot_read_account_or_create_authorization(self):
        self.write_model()
        with patch.object(cli,"make_api") as api, redirect_stdout(io.StringIO()) as output:
            code=cli.main(["arm","--root",str(self.root),"--mode","live"])
        self.assertEqual(code,2)
        self.assertIn("MODEL_NOT_RELEASED",output.getvalue())
        api.assert_not_called()
        self.assertFalse((cli.state_dir(self.root)/"ARM.json").exists())

    def test_release_flags_are_literal_booleans(self):
        base={"source":"Bitget recorded data","future_profitability_guaranteed":False,
              "cells":{"x":{"statistical_gate_passed":True}}}
        for key in ("native_bitget_data_verified","execution_and_protection_verified",
                    "prospective_validation_verified","deployment_approved"):
            base[key]=True
        cli.release_check(base)
        for value in (1,"true","false"):
            with self.assertRaises(SafetyError):
                cli.release_check({**base,"execution_and_protection_verified":value})
        with self.assertRaises(SafetyError):
            cli.release_check({**base,"source":"Binance proxy for Bitget"})

    def test_model_hash_and_content_are_one_artifact(self):
        self.write_model(marker="one")
        model,digest=cli.load_model(self.root)
        import hashlib
        self.assertEqual(digest,hashlib.sha256(cli.model_path(self.root).read_bytes()).hexdigest())
        self.assertEqual(model["marker"],"one")

    def test_pause_revokes_arm_without_erasing_positions(self):
        store=Store(cli.db_path(self.root,"live"))
        store.set("cci_chop_engine",{"positions":{"BTCUSDT":{"qty":.01}}})
        store.close()
        arm=cli.state_dir(self.root)/"ARM.json"
        arm.write_text("{}")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["pause","--root",str(self.root)]),0)
        self.assertFalse(arm.exists())
        self.assertTrue((cli.state_dir(self.root)/"PAUSE").exists())
        self.assertEqual(cli.read_status(self.root,"live")["positions"]["BTCUSDT"]["qty"],.01)

    def test_runner_lock_covers_all_modes(self):
        with cli.single_runner(self.root):
            with self.assertRaises(SafetyError):
                with cli.single_runner(self.root):
                    pass

    def test_status_never_creates_state_or_claims_exchange_validation(self):
        self.assertEqual(cli.read_status(self.root,"shadow")["status"],"NOT_STARTED")
        self.assertFalse(cli.state_dir(self.root).exists())

    def test_demo_file_cannot_reuse_live_or_environment_credentials(self):
        path=self.root/"demo.env"
        path.write_text("BITGET_API_KEY=live\nBITGET_SECRET_KEY=secret\nBITGET_PASSPHRASE=pass\n")
        with patch.object(cli,"credentials",return_value={"key":"live"}), \
             patch.object(cli,"connection",return_value={"env":"unused"}):
            with self.assertRaises(SafetyError):
                cli.strict_demo_credentials(self.root,path)
        path.write_text("BITGET_API_KEY=demo\n")
        with patch.dict("os.environ",{"BITGET_SECRET_KEY":"environment-live-secret","BITGET_PASSPHRASE":"environment-pass"}):
            with self.assertRaises(SafetyError):
                cli.strict_demo_credentials(self.root,path)

    def test_configuration_rejects_unsafe_caps_and_boolean_numbers(self):
        self.assertEqual(config.validate({**config.DEFAULT,"leverage":1})["leverage"],1)
        for key,value in (("leverage",True),("margin_cap_usdt",301),("risk_fraction",.011),
                          ("margin_reserve_usdt",149),("risk_fraction",float("nan"))):
            with self.assertRaises(SafetyError):
                config.validate({**config.DEFAULT,key:value})

    def test_manual_summary_uses_recent_running_observation(self):
        status={"runtime":{"running":True},"observation":{"at":100000,"entry_enabled":True},"positions":{}}
        self.assertTrue(cli.status_summary(status,at=100001)["entry_enabled"])
        self.assertFalse(cli.status_summary(status,at=131000)["entry_enabled"])
        self.assertFalse(cli.status_summary(status,paused=True,at=100001)["entry_enabled"])
        self.assertFalse(cli.status_summary({**status,"halt":"UNRESOLVED"},at=100001)["entry_enabled"])
        self.assertFalse(cli.status_summary({**status,"risk":{"active_blocks":["DAILY_LOSS_LIMIT"]}},at=100001)["entry_enabled"])

    def test_periodic_summary_does_not_advertise_halted_entries(self):
        engine=SimpleNamespace(state={"positions":{},"pending":{},"halt":"UNKNOWN","risk":{}})
        self.assertFalse(cli.summary_event(engine,True)["entry_enabled"])

    def test_telegram_modes_and_saved_record_are_distinct(self):
        event={"kind":"SUMMARY","state_only":True,"running":False}
        demo=render(event,"demo"); shadow=render(event,"shadow")
        self.assertIn("비트겟 데모매매",demo)
        self.assertIn("별도 Bitget 데모 계정",demo)
        self.assertNotIn("실제 Bitget 주문 0건",demo)
        self.assertIn("실제 Bitget 주문 0건",shadow)
        self.assertIn("저장된 서버 기록 기준",shadow)

    def test_summary_reports_macro_conflict_as_wait_without_inventing_probability(self):
        event={"kind":"SUMMARY","markets":{"BTCUSDT":{"bias":{"W":{"direction":1},"D":{"direction":-1}},
               "eligible_setup":False,"wait_reasons":["W_D_DIRECTION_CONFLICT"]}}}
        text=render(event,"shadow")
        self.assertIn("주 상승 / 일 하락",text)
        self.assertIn("주봉·일봉 방향 불일치",text)
        self.assertNotIn("승률",text)

    def test_trade_ids_deduplicate_restarts_but_not_identical_new_trades(self):
        path=cli.db_path(self.root,"shadow"); store=Store(path)
        notifier=Notifier(store,path,{"token":"unused","chat":"unused"},"shadow")
        first={"kind":"OPEN","id":"trade1","symbol":"BTCUSDT","entry":100,"qty":1,"opened":1000}
        notifier.queue(first); notifier.queue(first)
        notifier.queue({**first,"id":"trade2","opened":2000})
        notifier.queue({"kind":"GUARD_TIGHTENED","symbol":"BTCUSDT"})
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM cci_chop_telegram").fetchone()[0],2)
        notifier.close(); store.close()

    def test_shutdown_attempts_durable_final_notice_without_network(self):
        path=cli.db_path(self.root,"shadow"); store=Store(path)
        notifier=Notifier(store,path,{"token":"unused","chat":"unused"},"shadow")
        notifier.queue({"kind":"STOPPED","reason":"SHADOW_WEEK_COMPLETE"})
        with patch("cci_chop_v1.notifications.send") as send:
            notifier.start(); notifier.close()
        self.assertEqual(send.call_count,1)
        self.assertIsNotNone(store.db.execute("SELECT delivered FROM cci_chop_telegram").fetchone()[0])
        store.close()


if __name__=="__main__":
    unittest.main()
