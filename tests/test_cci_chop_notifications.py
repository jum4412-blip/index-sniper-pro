"""Meaningful Korean notice/privacy/durable delivery checks; no external messages."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cci_chop_v1._compat.store import Store
from cci_chop_v1.notifications import Notifier, render


INDICATORS = {"cci_frame": "H6", "chop_frame": "M3", "cci20": 132.125,
              "chop14": 31.875, "valid": True}


class NoticeTests(unittest.TestCase):
    def test_every_live_notice_states_missing_profitability_approval(self):
        for kind in ("OPEN", "CLOSE", "START", "SUMMARY", "HALT", "ORDER_UNKNOWN", "TEST", "STOPPED"):
            with self.subTest(kind=kind):
                message = render({"kind": kind, "at": 1000}, "live")
                self.assertIn("미검증 실전 · 수익성 승인 없음", message)
                self.assertNotIn("검증 완료", message)

    def test_open_explains_selected_frames_and_completed_values(self):
        message = render({"kind": "OPEN", "symbol": "BTCUSDT", "side": "LONG",
                          "entry": 100000, "qty": .001, "stop": 99500, "risk_usdt": 4.7,
                          "setup_meta": {"indicators": INDICATORS}}, "live")
        self.assertIn("BTCUSDT 롱", message)
        self.assertIn("CCI20(6시간봉) 132.12", message)
        self.assertIn("CHOP14(3분봉) 31.88", message)
        self.assertIn("완료봉 기준", message)
        self.assertIn("롱 CCI ≥ +100", message)
        self.assertNotIn("승률", message)

    def test_summary_reports_macro_and_two_filter_waits(self):
        message = render({"kind": "SUMMARY", "state_only": True, "running": False,
            "markets": {"ETHUSDT": {"bias": {"W": {"direction": -1}, "D": {"direction": 1}},
                "indicators": INDICATORS, "eligible_setup": False,
                "wait_reasons": ["W_D_DIRECTION_CONFLICT", "CCI20_SHORT_NOT_CONFIRMED", "CHOP14_NOT_TRENDING"]}}}, "shadow")
        self.assertIn("주 하락 / 일 상승", message)
        self.assertIn("주봉·일봉 방향 불일치", message)
        self.assertIn("숏 CCI20 -100 이하 확인 대기", message)
        self.assertIn("CHOP14 38.2 이하 추세 확인 대기", message)
        self.assertIn("프로세스 미실행 또는 관측 지연", message)
        self.assertIn("실제 Bitget 주문 0건", message)

    def test_unavailable_indicator_is_not_reported_as_valid(self):
        message = render({"kind": "OPEN", "setup_meta": {"indicators": {
            **INDICATORS, "valid": False, "chop14": None}}}, "demo")
        self.assertIn("CHOP14(3분봉) 확인 필요", message)
        self.assertNotIn("완료봉 기준", message)
        self.assertIn("별도 Bitget 데모 계정", message)

    def test_unknown_exception_and_malformed_number_do_not_leak(self):
        secret = "sensitive-api-secret"
        message = render({"kind": "ERROR", "reason": secret, "symbol": secret,
                          "entry": secret, "qty": float("nan"), "net_usdt": secret}, "live")
        self.assertNotIn(secret, message)
        self.assertNotIn("nan", message)
        self.assertIn("확인 필요", message)

    def test_unknown_order_notice_does_not_claim_resend(self):
        message = render({"kind": "ORDER_UNKNOWN"}, "live")
        self.assertIn("주문을 재전송하지 않습니다", message)
        self.assertIn("체결·보유 수량 확인", message)

    def test_process_stop_is_not_flat_position_claim(self):
        message = render({"kind": "STOPPED", "reason": "PROCESS_STOPPED"}, "live")
        self.assertIn("프로세스 종료는 포지션 청산이 아닙니다", message)


class DurableOutboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite"
        self.store = Store(self.path)
        self.notifier = Notifier(self.store, self.path, {"token": "not-used", "chat": "not-used"}, "live")

    def tearDown(self):
        self.notifier.close()
        self.store.close()
        self.directory.cleanup()

    def test_restarted_trade_notice_deduplicates_even_timestamp_changes(self):
        first = {"kind": "OPEN", "id": "trade-1", "symbol": "BTCUSDT", "qty": .001, "at": 1000}
        self.notifier.queue(first)
        self.notifier.queue({**first, "at": 2000})
        self.notifier.queue({**first, "id": "trade-2"})
        self.notifier.queue({"kind": "GUARD_TIGHTENED", "symbol": "BTCUSDT"})
        rows = self.store.db.execute("SELECT key FROM cci_chop_telegram ORDER BY key").fetchall()
        self.assertEqual(rows, [("OPEN:live:trade-1",), ("OPEN:live:trade-2",)])

    def test_success_is_durable_without_external_network(self):
        self.notifier.queue({"kind": "STOPPED"})
        with patch("cci_chop_v1.notifications.send") as sender:
            self.notifier.start()
            self.notifier.close()
        self.assertEqual(sender.call_count, 1)
        delivered, attempts = self.store.db.execute("SELECT delivered,attempts FROM cci_chop_telegram").fetchone()
        self.assertIsNotNone(delivered)
        self.assertEqual(attempts, 1)

    def test_delivery_failure_retains_retry_and_does_not_escape(self):
        self.notifier.queue({"kind": "START"})
        with patch("cci_chop_v1.notifications.send", side_effect=OSError("offline")) as sender:
            self.notifier.start()
            self.notifier.close()
        self.assertEqual(sender.call_count, 1)
        delivered, attempts, due = self.store.db.execute("SELECT delivered,attempts,due FROM cci_chop_telegram").fetchone()
        self.assertIsNone(delivered)
        self.assertEqual(attempts, 1)
        self.assertGreater(due, 0)


if __name__ == "__main__":
    unittest.main()
