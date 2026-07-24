from __future__ import annotations

import json
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from index_sniper import btc_structure_trend_v1 as m


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/btc_structure_trend_v1.json"


class FakeCandleClient:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, str]] = []

    def get(self, path, params, auth=False):
        assert path == "/api/v3/market/candles"
        self.calls.append(dict(params))
        end = int(params.get("endTime", 2**63 - 1))
        limit = int(params["limit"])
        eligible = [row for row in self.rows if int(row[0]) <= end]
        page = eligible[-limit:]
        return {"code": "00000", "data": page}


class FakePostClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def post(self, path, payload):
        self.calls.append((path, dict(payload)))
        return {"code": "00000", "msg": "success", "data": "success"}


class StrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = m.load_settings(CONFIG)
        cls.instrument = m.Instrument(
            symbol="BTCUSDT",
            status="online",
            symbol_type="crypto",
            min_order_qty=Decimal("0.0001"),
            max_order_qty=Decimal("100"),
            max_market_order_qty=Decimal("100"),
            min_order_amount=Decimal("5"),
            price_step=Decimal("0.1"),
            quantity_step=Decimal("0.0001"),
            min_leverage=1,
            max_leverage=125,
            maker_fee=0.0002,
            taker_fee=0.0006,
        )

    def test_contract_and_sizing(self) -> None:
        self.assertEqual(m.validate_settings_contract(self.settings), [])
        qty = m.calculate_qty(10_000, 100_000, self.settings, self.instrument)
        self.assertEqual(qty, Decimal("0.25"))
        self.assertAlmostEqual(float(qty) * 100_000 / 10_000, 2.5)

    def test_candle_pagination_collects_more_than_one_page(self) -> None:
        start = 1_700_000_000_000
        rows = [
            [str(start + i * 900_000), "100", "101", "99", "100.5", "10", "1005"]
            for i in range(260)
        ]
        fake = FakeCandleClient(rows)
        bars = m.fetch_candles(fake, "BTCUSDT", "15m", 230)
        self.assertEqual(len(bars), 230)
        self.assertGreaterEqual(len(fake.calls), 3)
        self.assertEqual(bars[0].ts, start + 30 * 900_000)
        self.assertEqual(bars[-1].ts, start + 259 * 900_000)

    def test_volume_profile_and_nearest_zone(self) -> None:
        bars = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(180):
            center = 100 if i < 120 else 110
            bars.append(
                m.Bar(
                    ts=int((start + timedelta(hours=i)).timestamp() * 1000),
                    open=center - 0.2,
                    high=center + 0.8,
                    low=center - 0.8,
                    close=center + 0.2,
                    volume=500 if i < 120 else 100,
                )
            )
        zones = m.volume_profile_zones(bars, bins=48, high_volume_quantile=0.7)
        self.assertTrue(zones)
        support = m.nearest_support(zones, 105)
        self.assertIsNotNone(support)
        assert support is not None
        self.assertLess(support.center, 105)
        self.assertLessEqual(max(z.relative_volume for z in zones), 1.0)

    def test_opening_payload_has_hard_stop_and_no_fixed_tp(self) -> None:
        candidate = m.Candidate(
            symbol="BTCUSDT",
            side="LONG",
            signal_bar_ts=1,
            entry_reference=100_000,
            zone_low=99_200,
            zone_high=99_600,
            zone_center=99_400,
            soft_stop=99_100,
            hard_stop=98_500,
            initial_risk=900,
            stop_distance_pct=0.9,
            score=82,
            setup="TEST",
            diagnostics={},
        )
        payload = m.opening_payload(candidate, Decimal("0.25"), self.instrument, self.settings, "bst_test")
        self.assertEqual(payload["marginMode"], "crossed")
        self.assertEqual(payload["posSide"], "long")
        self.assertEqual(payload["stopLoss"], "98500")
        self.assertEqual(payload["slTriggerBy"], "mark")
        self.assertNotIn("takeProfit", payload)

    def test_cross_leverage_payload_uses_bitget_crossed_enum(self) -> None:
        fake = FakePostClient()
        result = m.set_cross_leverage(fake, "BTCUSDT", 5)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(fake.calls), 1)
        path, payload = fake.calls[0]
        self.assertEqual(path, "/api/v3/account/set-leverage")
        self.assertEqual(payload["marginMode"], "crossed")
        self.assertEqual(payload["leverage"], "5")
        self.assertNotIn("posSide", payload)
        self.assertNotIn("longLeverage", payload)
        self.assertNotIn("shortLeverage", payload)

    def test_strong_break_requires_quality_or_two_closes(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = []
        for i in range(28):
            bars.append(
                m.Bar(
                    ts=int((start + timedelta(minutes=15 * i)).timestamp() * 1000),
                    open=100.2,
                    high=100.6,
                    low=99.8,
                    close=100.1,
                    volume=100,
                )
            )
        bars.append(m.Bar(int((start + timedelta(minutes=15 * 28)).timestamp() * 1000), 100.1, 100.2, 99.7, 99.9, 100))
        bars.append(m.Bar(int((start + timedelta(minutes=15 * 29)).timestamp() * 1000), 99.8, 99.9, 98.4, 98.5, 400))
        managed = m.ManagedPosition(
            symbol="BTCUSDT",
            side="LONG",
            qty=0.1,
            entry_price=101,
            entry_ts=m.iso(),
            entry_order_id="1",
            entry_client_oid="bst_1",
            hold_mode="hedge_mode",
            initial_soft_stop=99.5,
            soft_stop=99.5,
            hard_stop=98,
            initial_risk=1.5,
            zone_low=99.5,
            zone_high=100,
            best_price=103,
        )
        broken, diag = m.strong_structure_break(managed, bars, self.settings)
        self.assertTrue(broken)
        self.assertTrue(diag["one_strong"] or diag["two_consecutive_closes"])

    def test_trailing_stop_never_loosened(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = []
        price = 100.0
        for i in range(220):
            open_price = price
            close = price + 0.08 + math.sin(i / 9) * 0.03
            bars.append(
                m.Bar(
                    ts=int((start + timedelta(minutes=15 * i)).timestamp() * 1000),
                    open=open_price,
                    high=max(open_price, close) + 0.4,
                    low=min(open_price, close) - 0.4,
                    close=close,
                    volume=100 + i % 7,
                )
            )
            price = close
        managed = m.ManagedPosition(
            symbol="BTCUSDT",
            side="LONG",
            qty=1,
            entry_price=100,
            entry_ts=m.iso(),
            entry_order_id="1",
            entry_client_oid="bst_1",
            hold_mode="hedge_mode",
            initial_soft_stop=99,
            soft_stop=101,
            hard_stop=98,
            initial_risk=1,
            zone_low=99,
            zone_high=100,
            best_price=price,
        )
        new_stop, active, _ = m.update_trailing_stop(managed, bars, self.settings, price)
        self.assertTrue(active)
        self.assertGreaterEqual(new_stop, managed.soft_stop)
        self.assertLess(new_stop, price)

    def test_changed_contract_is_rejected(self) -> None:
        changed = replace(self.settings, entry_margin_pct=40)
        errors = m.validate_settings_contract(changed)
        self.assertTrue(any("entry_margin_pct" in error for error in errors))

        isolated = replace(self.settings, margin_mode="isolated")
        errors = m.validate_settings_contract(isolated)
        self.assertTrue(any("expected crossed" in error for error in errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
