"""Venue evidence and local accounting notices; no Telegram/network access."""
from pathlib import Path
import tempfile
import unittest

from cci_chop_v1._compat.store import Store
from cci_chop_v1.notifications import Notifier, render


class EvidenceNoticeTests(unittest.TestCase):
    def test_flat_exchange_and_local_unsettled_record_are_separate(self):
        text = render({"kind": "SUMMARY", "at": 100000,
                       "venue_position_count": 0,
                       "venue_positions_verified_at_ms": 99000,
                       "position_count": 1, "accounting_pending_count": 1,
                       "positions": {"BTCUSDT": {"qty": .0104, "side": "LONG", "stop": 81578.5}},
                       "latest_management_error": {"reason": "NATIVE_CHILD_OWNERSHIP_MISMATCH"}}, "live")
        self.assertIn("거래소 확인 보유 0종목", text)
        self.assertIn("저장된 포지션 기록 1종목", text)
        self.assertIn("청산 정산 대기 기록 1종목", text)
        self.assertIn("NATIVE_CHILD_OWNERSHIP_MISMATCH", text)
        self.assertNotIn("최근 관측 정상", text)

    def test_expired_or_future_read_never_claims_actual_holdings(self):
        for stamp in (None, 0, 69999, 100001, float("nan"), True):
            with self.subTest(stamp=stamp):
                text = render({"kind": "SUMMARY", "at": 100000,
                               "venue_position_count": 0,
                               "venue_positions_verified_at_ms": stamp,
                               "position_count": 1}, "live")
                self.assertIn("실제 보유: 확인 필요", text)
                self.assertNotIn("거래소 확인 보유 0종목", text)

    def test_legacy_local_state_does_not_claim_exchange_ownership(self):
        text = render({"kind": "SUMMARY", "position_count": 1,
                       "state_only": True, "running": True}, "live")
        self.assertIn("저장된 포지션 기록 1종목", text)
        self.assertIn("포지션 관리 정상 여부는 별도", text)
        self.assertNotIn("거래소 확인 보유", text)

    def test_open_distinguishes_requested_partial_fill_and_unverified_stop(self):
        text = render({"kind": "OPEN", "entry": 82390.5, "qty": .0104,
                       "requested_qty": .0182, "stop": 81578.5,
                       "target_margin_usdt": 300, "leverage": 5,
                       "risk_usdt": 10.41}, "live")
        self.assertIn("요청 수량: 0.01820000", text)
        self.assertIn("체결 수량: 0.01040000", text)
        self.assertIn("목표 명목 증거금: 300.00", text)
        self.assertIn("체결 시 포지션 금액: 856.86", text)
        self.assertIn("체결 시 명목 증거금: 171.37", text)
        self.assertIn("구조 보호선(거래소 확인 별도)", text)
        self.assertIn("예상 위험(상한 아님)", text)
        self.assertNotIn("거래소 확인된 구조 보호선", text)

    def test_explicit_protection_readback_can_be_reported(self):
        text = render({"kind": "OPEN", "stop": 81578.5,
                       "protection_verified": True}, "live")
        self.assertIn("거래소 확인된 구조 보호선", text)

    def test_unknown_error_codes_are_preserved_without_exception_secrets(self):
        text = render({"kind": "HALT", "reason": "NEW_NATIVE_PROTOCOL_ERROR"}, "live")
        self.assertIn("NEW_NATIVE_PROTOCOL_ERROR", text)
        secret = "key-secret / failed token=private"
        text = render({"kind": "HALT", "reason": secret}, "live")
        self.assertNotIn(secret, text)
        text = render({"kind": "HALT", "reason": "MODEL_NOT_RELEASED: " + secret}, "live")
        self.assertIn("MODEL_NOT_RELEASED", text)
        self.assertNotIn(secret, text)

    def test_native_fill_is_not_final_settlement_claim(self):
        text = render({"kind": "NATIVE_STOP_FILL_CONFIRMED", "symbol": "BTCUSDT",
                       "qty": .0104, "price": 81571.3, "remaining_qty": 0}, "live")
        self.assertIn("구조 보호 주문 청산 체결 확인", text)
        self.assertIn("청산 체결가: 81571.3000", text)
        self.assertIn("최종 손익 정산 확인은 별도", text)


class RecoveryNoticeDeliveryTests(unittest.TestCase):
    def test_native_fill_notice_deduplicates_restart_but_not_later_partial_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = Store(path)
            notifier = Notifier(store, path, {"token": "unused", "chat": "unused"}, "live")
            try:
                first = {"kind": "NATIVE_STOP_FILL_CONFIRMED", "child_id": "child-1",
                         "qty": .0052, "remaining_qty": .0052, "at": 1000}
                notifier.queue(first)
                notifier.queue({**first, "at": 2000})
                notifier.queue({**first, "remaining_qty": 0, "at": 3000})
                notifier.queue({"kind": "MANAGEMENT_BLOCKED", "reason": "READ_FAILED"})
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM cci_chop_telegram").fetchone()[0], 2)
            finally:
                notifier.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
