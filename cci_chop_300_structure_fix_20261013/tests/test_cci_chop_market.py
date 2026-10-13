import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from cci_chop_v1._compat.core import DataError
from cci_chop_v1.market import (
    DAY_MS, H1_MS, H4_MS, HISTORY_PATH, M5_MS, MAX_HISTORY_PAGES,
    MONDAY_ANCHOR_MS, RECENT_PATH, WEEK_MS, MarketFrames, aggregate_rows,
)


NOW = int(datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp() * 1000)


def row(stamp):
    price = 100 + (stamp // M5_MS) % 31
    return [str(stamp), str(price), str(price + 2), str(price - 2), str(price + 1), "10", "1000"]


def series(now, interval, count=1200):
    latest = (now // interval) * interval
    return [row(latest - i * interval) for i in range(count - 1, -1, -1)]


class FakeAPI:
    def __init__(self, now=NOW, recent_cap=1000, order="ascending"):
        self.now = now
        self.recent_cap = recent_cap
        self.order = order
        self.calls = []
        self.instrument_calls = []
        self.change = None
        self.history_empty = False
        self.history_no_progress = False
        self.history_one_only = False
        self.post_calls = 0
        self.data = {"4H": series(now, H4_MS), "1H": series(now, H1_MS), "5m": series(now, M5_MS)}

    def advance(self, amount):
        self.now += amount
        self.data = {"4H": series(self.now, H4_MS), "1H": series(self.now, H1_MS), "5m": series(self.now, M5_MS)}

    def get(self, path, params=None, private=False):
        if private or path not in (RECENT_PATH, HISTORY_PATH):
            raise AssertionError("public candle GET required")
        self.calls.append((path, dict(params), private))
        data = copy.deepcopy(self.data[params["interval"]])
        if path == HISTORY_PATH:
            if self.history_empty:
                return []
            if self.history_no_progress:
                return data[-100:]
            data = [r for r in data if int(r[0]) <= int(params["endTime"]) and int(r[0]) >= int(params["startTime"])]
            data = data[-(1 if self.history_one_only else int(params["limit"])):]
        else:
            data = data[-min(self.recent_cap, int(params["limit"])):]
        if self.change:
            data = self.change(path, params, data)
        if self.order == "descending":
            data.reverse()
        elif self.order == "mixed":
            data = data[::2] + data[1::2]
        return data

    def instrument(self, symbol):
        self.instrument_calls.append(symbol)
        return SimpleNamespace(price_step="0.1" if symbol == "BTCUSDT" else "0.01")

    def post(self, *args, **kwargs):
        self.post_calls += 1
        raise AssertionError("market transport must not write")


class MarketFrameTests(unittest.TestCase):
    def test_exact_completed_frame_lengths_and_monday_alignment(self):
        api = FakeAPI(order="descending")
        frames = MarketFrames(api).get("BTCUSDT", NOW)
        for key, count in (("W", 20), ("D", 90), ("H4", 120), ("H1", 72), ("M5", 64)):
            self.assertEqual(len(frames[key]), count)
        self.assertEqual(frames["W"][-1][0], NOW // WEEK_MS * WEEK_MS + MONDAY_ANCHOR_MS - WEEK_MS)
        self.assertTrue(all((r[0] - MONDAY_ANCHOR_MS) % WEEK_MS == 0 for r in frames["W"]))
        self.assertEqual(frames["D"][-1][0], NOW // DAY_MS * DAY_MS - DAY_MS)
        self.assertEqual(frames["H4"][-1][0], NOW - H4_MS)
        self.assertEqual(frames["tick_size"], .1)
        self.assertEqual(sum(path == HISTORY_PATH for path, _, _ in api.calls), 0)
        self.assertEqual({p["interval"] for _, p, _ in api.calls}, {"4H", "1H", "5m"})

    def test_history_pagination_completes_short_recent_warmup(self):
        api = FakeAPI(recent_cap=540, order="mixed")
        frames = MarketFrames(api).get("ETHUSDT", NOW)
        history = [params for path, params, _ in api.calls if path == HISTORY_PATH]
        self.assertEqual(len(history), 4)
        self.assertEqual(frames["tick_size"], .01)
        self.assertTrue(all(p["limit"] == "100" for p in history))
        self.assertTrue(all(0 < int(p["endTime"]) - int(p["startTime"]) < 90 * DAY_MS for p in history))
        self.assertTrue(all(int(b["endTime"]) < int(a["endTime"]) for a, b in zip(history, history[1:])))

    def test_same_bucket_cache_is_copied_and_rollover_retains_old_history(self):
        api = FakeAPI(recent_cap=540)
        market = MarketFrames(api)
        frames = market.get("BTCUSDT", NOW)
        calls = len(api.calls)
        frames["H4"][0][4] = -1
        again = market.get("BTCUSDT", NOW + 1)
        self.assertEqual(len(api.calls), calls)
        self.assertGreater(again["H4"][0][4], 0)
        previous_pages = sum(path == HISTORY_PATH for path, _, _ in api.calls)
        api.advance(H4_MS)
        api.recent_cap = 100
        updated = market.get("BTCUSDT", NOW + H4_MS)
        self.assertEqual(updated["H4"][-1][0], NOW)
        self.assertEqual(sum(path == HISTORY_PATH for path, _, _ in api.calls), previous_pages)

    def test_duplicate_identical_closed_rows_and_forming_updates_are_safe(self):
        api = FakeAPI()
        def add_duplicates(path, params, data):
            return data + [data[-2][:], [str(api.now // {"4H": H4_MS, "1H": H1_MS, "5m": M5_MS}[params["interval"]] * {"4H": H4_MS, "1H": H1_MS, "5m": M5_MS}[params["interval"]]), "nan"]]
        api.change = add_duplicates
        frames = MarketFrames(api).get("BTCUSDT", NOW)
        self.assertEqual(len(frames["M5"]), 64)

    def test_conflicting_closed_duplicate_fails(self):
        api = FakeAPI()
        def duplicate(path, params, data):
            changed = data[-2][:]
            changed[5] = "11"
            return data + [changed]
        api.change = duplicate
        with self.assertRaisesRegex(DataError, "CLOSED_CANDLE_CONFLICT"):
            MarketFrames(api).get("BTCUSDT", NOW)

    def test_changed_closed_cached_history_fails_on_rollover(self):
        api = FakeAPI()
        market = MarketFrames(api)
        market.get("BTCUSDT", NOW)
        api.advance(H4_MS)
        api.data["4H"][-3][5] = "999"
        with self.assertRaisesRegex(DataError, "CLOSED_CANDLE_CONFLICT"):
            market.get("BTCUSDT", NOW + H4_MS)

    def test_missing_latest_bar_is_not_hidden_by_cache(self):
        api = FakeAPI()
        market = MarketFrames(api)
        market.get("BTCUSDT", NOW)
        api.advance(H4_MS)
        api.data["4H"] = api.data["4H"][:-2]
        with self.assertRaisesRegex(DataError, "LATEST_CLOSED_CANDLE_MISSING"):
            market.get("BTCUSDT", NOW + H4_MS)

    def test_gap_in_required_h4_history_fails(self):
        api = FakeAPI()
        del api.data["4H"][-700]
        with self.assertRaisesRegex(DataError, "CANDLE_GAP"):
            MarketFrames(api).get("BTCUSDT", NOW)

    def test_h1_and_m5_gap_or_stale_are_refused(self):
        for interval in ("1H", "5m"):
            with self.subTest(interval=interval):
                api = FakeAPI()
                del api.data[interval][-20]
                with self.assertRaisesRegex(DataError, "CANDLE_GAP"):
                    MarketFrames(api).get("BTCUSDT", NOW)
                api = FakeAPI()
                api.data[interval] = api.data[interval][:-2]
                with self.assertRaisesRegex(DataError, "LATEST_CLOSED_CANDLE_MISSING"):
                    MarketFrames(api).get("BTCUSDT", NOW)

    def test_unknown_history_completeness_and_page_limit_are_refused(self):
        for attribute, reason in (("history_empty", "FRAME_WARMUP_INCOMPLETE"), ("history_no_progress", "FRAME_WARMUP_INCOMPLETE"), ("history_one_only", "FRAME_WARMUP_INCOMPLETE")):
            with self.subTest(attribute=attribute):
                api = FakeAPI(recent_cap=540)
                setattr(api, attribute, True)
                with self.assertRaisesRegex(DataError, reason):
                    MarketFrames(api).get("BTCUSDT", NOW)
                self.assertLessEqual(sum(path == HISTORY_PATH for path, _, _ in api.calls), MAX_HISTORY_PAGES)

    def test_closed_prices_volume_alignment_and_symbol_are_validated(self):
        mutations = ((0, "1"), (1, "nan"), (2, "0"), (5, "-1"))
        for field, value in mutations:
            with self.subTest(field=field):
                api = FakeAPI()
                api.data["4H"][-2][field] = value
                with self.assertRaises(DataError):
                    MarketFrames(api).get("BTCUSDT", NOW)
        with self.assertRaises(DataError):
            MarketFrames(FakeAPI()).get("UNKNOWN", NOW)

    def test_partial_missing_macro_history_retains_safe_management_frames(self):
        api = FakeAPI(recent_cap=540)
        api.history_empty = True
        market = MarketFrames(api)
        with self.assertRaisesRegex(DataError, "FRAME_WARMUP_INCOMPLETE"):
            market.get("BTCUSDT", NOW)
        frames = market.get_partial("BTCUSDT", NOW)
        self.assertEqual(set(frames), {"H4", "H1", "M5", "tick_size", "_errors"})
        self.assertEqual(set(frames["_errors"]), {"W", "D"})
        self.assertEqual(len(frames["H4"]), 120)
        self.assertEqual(len(frames["H1"]), 72)
        self.assertEqual(len(frames["M5"]), 64)
        self.assertEqual(api.post_calls, 0)

    def test_partial_daily_is_available_when_only_week_warmup_is_missing(self):
        api = FakeAPI(recent_cap=700)
        api.history_empty = True
        frames = MarketFrames(api).get_partial("BTCUSDT", NOW)
        self.assertNotIn("W", frames)
        self.assertEqual(len(frames["D"]), 90)
        self.assertEqual(set(frames["_errors"]), {"W"})

    def test_strict_and_partial_agree_when_900_bars_supply_all_spec_frames(self):
        api = FakeAPI(recent_cap=900)
        api.history_empty = True
        market = MarketFrames(api)
        partial = market.get_partial("BTCUSDT", NOW)
        self.assertEqual(partial["_errors"], {})
        calls = len(api.calls)
        strict = market.get("BTCUSDT", NOW)
        self.assertEqual(strict, {key: value for key, value in partial.items() if key != "_errors"})
        self.assertEqual(len(api.calls), calls)
        self.assertEqual({key: len(strict[key]) for key in ("W", "D", "H4", "H1", "M5")}, {"W": 20, "D": 90, "H4": 120, "H1": 72, "M5": 64})
        self.assertEqual(api.post_calls, 0)

    def test_partial_h1_failure_preserves_m5_guard_breach_evidence(self):
        api = FakeAPI()
        api.data["5m"][-2][1:5] = ["100", "101", "89", "90"]
        def fail_h1(path, params, data):
            if params["interval"] == "1H":
                raise DataError("untrusted transport body with access key")
            return data
        api.change = fail_h1
        frames = MarketFrames(api).get_partial("BTCUSDT", NOW)
        self.assertNotIn("H1", frames)
        self.assertEqual(frames["_errors"]["H1"], "CC_PUBLIC_READ_FAILED_DATAERROR")
        self.assertLess(frames["M5"][-1][4], 95)  # A held long's known guard.
        self.assertEqual(api.post_calls, 0)

    def test_partial_never_reuses_stale_conflicting_or_gapped_h4(self):
        for failure in ("stale", "conflict", "gap"):
            with self.subTest(failure=failure):
                api = FakeAPI()
                market = MarketFrames(api)
                market.get("BTCUSDT", NOW)
                api.advance(H4_MS)
                if failure == "stale":
                    api.data["4H"] = api.data["4H"][:-2]
                elif failure == "conflict":
                    api.data["4H"][-3][5] = "999"
                else:
                    del api.data["4H"][-20]
                frames = market.get_partial("BTCUSDT", NOW + H4_MS)
                self.assertNotIn("H4", frames)
                self.assertNotIn("W", frames)
                self.assertNotIn("D", frames)
                self.assertEqual(len(frames["M5"]), 64)
                self.assertIn("H4", frames["_errors"])

    def test_partial_failure_retries_are_bounded_by_m5_bucket_and_result_is_copied(self):
        api = FakeAPI(recent_cap=540)
        api.history_empty = True
        market = MarketFrames(api)
        frames = market.get_partial("BTCUSDT", NOW)
        calls = len(api.calls)
        frames["M5"][-1][4] = -1
        frames["_errors"]["W"] = "changed"
        again = market.get_partial("BTCUSDT", NOW + 1)
        self.assertEqual(len(api.calls), calls)
        self.assertGreater(again["M5"][-1][4], 0)
        self.assertNotEqual(again["_errors"]["W"], "changed")
        with self.assertRaises(DataError):
            market.get("BTCUSDT", NOW + 1)
        self.assertEqual(len(api.calls), calls)
        old_pages = sum(path == HISTORY_PATH for path, _, _ in api.calls)
        api.advance(M5_MS)
        market.get_partial("BTCUSDT", NOW + M5_MS)
        self.assertEqual(sum(path == HISTORY_PATH for path, _, _ in api.calls), old_pages + 1)

    def test_partial_tick_failure_is_independent_and_strict_get_still_refuses(self):
        api = FakeAPI()
        api.instrument = lambda symbol: SimpleNamespace(price_step="nan")
        market = MarketFrames(api)
        frames = market.get_partial("BTCUSDT", NOW)
        self.assertNotIn("tick_size", frames)
        self.assertEqual(frames["_errors"], {"tick_size": "CC_TICK_SIZE_INVALID"})
        self.assertEqual(len(frames["M5"]), 64)
        with self.assertRaisesRegex(DataError, "TICK_SIZE_INVALID"):
            market.get("BTCUSDT", NOW)


class AggregationTests(unittest.TestCase):
    def test_partial_utc_day_edges_are_dropped(self):
        start = NOW // DAY_MS * DAY_MS - 3 * DAY_MS
        rows = [row(stamp) for stamp in range(start + H4_MS, start + 3 * DAY_MS + 3 * H4_MS, H4_MS)]
        result = aggregate_rows(rows, H4_MS, DAY_MS, start + 3 * DAY_MS + 3 * H4_MS)
        self.assertEqual([r[0] for r in result], [start + DAY_MS, start + 2 * DAY_MS])
        source = rows[5:11]
        self.assertEqual(result[0][1], float(source[0][1]))
        self.assertEqual(result[0][4], float(source[-1][4]))
        self.assertEqual(result[0][5], 60)

    def test_monday_week_partial_edges_and_forecast_invariance(self):
        monday = ((NOW - MONDAY_ANCHOR_MS) // WEEK_MS) * WEEK_MS + MONDAY_ANCHOR_MS
        rows = [row(stamp) for stamp in range(monday - 2 * WEEK_MS + 2 * DAY_MS, monday + 3 * DAY_MS, DAY_MS)]
        baseline = aggregate_rows(rows, DAY_MS, WEEK_MS, NOW, MONDAY_ANCHOR_MS)
        self.assertEqual([r[0] for r in baseline], [monday - WEEK_MS])
        future = rows + [[str(monday + 7 * DAY_MS), "invalid", "nan"]]
        self.assertEqual(aggregate_rows(future, DAY_MS, WEEK_MS, NOW, MONDAY_ANCHOR_MS), baseline)

    def test_completed_bar_exact_boundary_is_included(self):
        start = NOW // DAY_MS * DAY_MS
        rows = [row(start + i * H4_MS) for i in range(6)]
        self.assertEqual(len(aggregate_rows(rows, H4_MS, DAY_MS, start + DAY_MS)), 1)
        self.assertEqual(aggregate_rows(rows, H4_MS, DAY_MS, start + DAY_MS - 1), [])

    def test_gap_is_never_filled_or_hidden_in_aggregation(self):
        rows = series(NOW, H4_MS, 40)
        del rows[15]
        with self.assertRaisesRegex(DataError, "CANDLE_GAP"):
            aggregate_rows(rows, H4_MS, DAY_MS, NOW)


if __name__ == "__main__":
    unittest.main()
