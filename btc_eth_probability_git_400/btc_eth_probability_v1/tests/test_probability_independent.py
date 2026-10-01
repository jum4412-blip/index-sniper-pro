"""Independent replay probes; no exchange calls, orders, or message sending."""
import sys
import json
import sqlite3
import tempfile
import unittest
import urllib.error
import copy
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from outcome_labels import label_events
from probability_strategy import (build_events, fit_probability_model, group_key,
                                   nonoverlapping_training_labels, state_key)
from basic_core import notify
from basic_core.store import Store
from basic_core.core import CAT, DataError, Instrument
from basic_core.engine import Live
from basic_core.api import Rest


class IndependentOutcomeTests(unittest.TestCase):
    def market(self, side=1, price=30_000.0):
        origin = 1_577_836_800_000
        minute = np.zeros((80, 7), dtype=np.float64)
        minute[:, 0] = origin + np.arange(len(minute)) * 60_000
        minute[:, 1:5] = price
        minute[:, 5:] = 1.0
        events = pd.DataFrame([dict(entry_after_ms=origin + 2 * 60_000,
                                    side=side, entry_reference=price,
                                    stop=price * (1 - side * .004),
                                    max_hold_minutes=60)])
        return origin, minute, events

    def test_label_does_not_change_when_data_after_exit_changes(self):
        origin, minute, events = self.market()
        minute[3, 3] = 29_760
        original = label_events(minute, np.empty((0, 2)), events, 'BTCUSDT')
        revised = minute.copy()
        revised[4:, 1:5] = 300_000
        changed = label_events(revised, np.empty((0, 2)), events, 'BTCUSDT')
        self.assertEqual(original.reason.iloc[0], 'STOP')
        self.assertLess(original.label_end_ms.iloc[0], origin + 4 * 60_000)
        for key in ('entry_fill', 'qty', 'initial_stop', 'risk', 'label_end_ms',
                    'exit_reference', 'gross', 'fees', 'net', 'net_r'):
            self.assertEqual(original[key].iloc[0], changed[key].iloc[0], key)

    def test_funding_at_native_stop_timestamp_is_booked_before_close(self):
        origin, minute, events = self.market()
        minute[3, 3] = 29_760
        baseline = label_events(minute, np.empty((0, 2)), events, 'BTCUSDT').iloc[0]
        funds = np.array([[baseline.label_end_ms, .0001]], dtype=np.float64)
        replay = label_events(minute, funds, events, 'BTCUSDT').iloc[0]
        self.assertEqual(replay.label_end_ms, baseline.label_end_ms)
        self.assertAlmostEqual(replay.funding, -replay.qty * replay.initial_stop * .0001, places=8)
        self.assertAlmostEqual(replay.net, baseline.net + replay.funding, places=8)

    def test_net_r_denominator_contains_execution_loss_and_fees(self):
        for side in (1, -1):
            _, minute, events = self.market(side)
            minute[3, 3 if side == 1 else 2] = 29_700 if side == 1 else 30_300
            replay = label_events(minute, np.empty((0, 2)), events, 'BTCUSDT').iloc[0]
            self.assertEqual(replay.reason, 'STOP')
            self.assertAlmostEqual(replay.net_r, -1.0, places=7)
            self.assertAlmostEqual(replay.net, replay.gross - replay.fees + replay.funding, places=9)
            self.assertGreater(replay.risk, replay.qty * abs(replay.entry_fill - replay.initial_stop))

    def test_risk_failure_does_not_downsize_to_force_eligibility(self):
        _, minute, events = self.market()
        events.loc[0, 'stop'] = 30_000 * (1 - .008)
        replay = label_events(minute, np.empty((0, 2)), events, 'BTCUSDT').iloc[0]
        self.assertEqual(replay.execution_eligible, 0)
        self.assertAlmostEqual(replay.qty, np.floor(2500 / replay.entry_fill / .0001 + 1e-10) * .0001)
        self.assertGreater(replay.notional if replay.notional else replay.qty * replay.entry_fill, 2490)

    def test_last_known_funding_gate_does_not_use_future_rate(self):
        origin, minute, events = self.market()
        funds = np.array([[origin, .0001], [origin + 20 * 60_000, .001]], dtype=np.float64)
        replay = label_events(minute, funds, events, 'BTCUSDT').iloc[0]
        self.assertEqual(replay.execution_eligible, 1)
        self.assertEqual(replay.reason, 'MAX_HOLD')
        self.assertLess(replay.funding, 0)


class IndependentProbabilityTests(unittest.TestCase):
    def labels(self, times, outcomes=None, horizon=60_000):
        times = np.asarray(times, dtype=np.int64)
        if outcomes is None:
            outcomes = np.where(np.arange(len(times)) % 7 == 0, -1., 1.5)
        return pd.DataFrame({'symbol': 'BTCUSDT', 'entry_after_ms': times,
                             'label_end_ms': times + horizon,
                             'net_r': outcomes,
                             'label_profitable': np.asarray(outcomes) > 0,
                             'event_type': 'sweep_reclaim', 'side': 1,
                             'group_key': group_key('BTCUSDT', 'sweep_reclaim', 1),
                             'state_key': state_key('BTCUSDT', 'sweep_reclaim', 1, 1, 1, 1),
                             'structure_valid': True,
                             'execution_eligible': 1})

    def test_future_extreme_candles_do_not_change_earlier_event_features(self):
        origin = 1_577_836_800_000
        prefix = np.zeros((220, 7), dtype=np.float64)
        prefix[:, 0] = origin + np.arange(len(prefix)) * 300_000
        prefix[:, 1:5] = [1_000, 1_001, 999, 1_000]
        prefix[:, 5] = np.arange(len(prefix)) % 17 + 1
        prefix[:, 6] = prefix[:, 5] * 1_000
        prefix[200, 1:5] = [1_000, 1_002, 997, 1_001]
        events = build_events('BTCUSDT', prefix)
        self.assertGreater(len(events), 0)
        future = np.zeros((50, 7), dtype=np.float64)
        future[:, 0] = origin + np.arange(220, 270) * 300_000
        future[:, 1:5] = [100_000, 110_000, 90_000, 105_000]
        future[:, 5:] = 1e9
        later = build_events('BTCUSDT', np.vstack([prefix, future]))
        earlier = later.loc[later.bar_index < len(prefix)].reset_index(drop=True)
        pd.testing.assert_frame_equal(events.reset_index(drop=True), earlier)

    def test_overlapping_labels_are_thinned_and_not_counted_as_executed_trades(self):
        start = 1_577_836_800_000
        source = self.labels(start + np.arange(30) * 300_000, horizon=3_600_000)
        kept = nonoverlapping_training_labels(source, start + 10 * 86_400_000)
        self.assertEqual(kept.entry_after_ms.tolist(), source.entry_after_ms.iloc[[0, 13, 26]].tolist())
        self.assertLess(len(kept), len(source))

    def test_unresolved_and_purge_boundary_labels_never_enter_fit(self):
        cutoff = 1_577_923_200_000
        source = self.labels([cutoff - 4_000_000, cutoff - 3_700_000,
                              cutoff - 3_660_000, cutoff - 10_000])
        kept = nonoverlapping_training_labels(source, cutoff)
        self.assertTrue((kept.label_end_ms < cutoff - 3_600_000).all())
        self.assertNotIn(cutoff - 3_660_000, kept.entry_after_ms.tolist())
        self.assertNotIn(cutoff - 10_000, kept.entry_after_ms.tolist())

    def test_hundreds_of_labels_on_one_day_cannot_satisfy_day_support(self):
        start = 1_577_836_800_000
        source = self.labels(start + np.arange(400) * 120_000, horizon=60_000)
        model = fit_probability_model(source, start + 3 * 86_400_000)
        leaf = next(iter(model['states'].values()))
        self.assertGreaterEqual(leaf['n_state'], 300)
        self.assertEqual(leaf['days'], 1)
        self.assertFalse(leaf['eligible'])
        self.assertEqual(leaf['reason'], 'INSUFFICIENT_SUPPORT')

    def test_rejected_execution_is_excluded_before_probability_support(self):
        start = 1_577_836_800_000
        source = self.labels(start + np.arange(3) * 7_200_000)
        source.loc[0, 'execution_eligible'] = 0
        source.loc[0, 'net_r'] = 0.
        source.loc[0, 'label_profitable'] = False
        retained = nonoverlapping_training_labels(source, start + 3 * 86_400_000)
        self.assertEqual(len(retained), 2)
        self.assertNotIn(start, retained.entry_after_ms.tolist())

    def test_duplicate_dataframe_indices_do_not_multiply_probability_support(self):
        start = 1_577_836_800_000
        source = self.labels(start + np.arange(12) * 7_200_000)
        source.index = [0] * len(source)
        retained = nonoverlapping_training_labels(source, start + 3 * 86_400_000)
        self.assertEqual(len(retained), 12)
        self.assertEqual(retained.entry_after_ms.nunique(), 12)

    def test_break_even_uses_net_win_loss_sizes_not_nominal_two_r_target(self):
        start = 1_577_836_800_000
        times = np.array([start + day * 86_400_000 + slot * 7_200_000
                          for day in range(100) for slot in range(4)])
        source = self.labels(times)
        model = fit_probability_model(source, start + 102 * 86_400_000)
        leaf = next(iter(model['states'].values()))
        self.assertAlmostEqual(leaf['break_even'], 1 / 2.5, places=12)
        self.assertNotAlmostEqual(leaf['break_even'], 1 / 3, places=3)
        self.assertTrue(leaf['eligible'])
        self.assertGreaterEqual(leaf['p05'], leaf['break_even'] + .02)
        self.assertGreater(leaf['lower95'], 0)


class IndependentTelegramTests(unittest.TestCase):
    def test_heartbeat_renders_runtime_status_schema_and_both_assets(self):
        text = notify.render('HEARTBEAT', {
            'entry_enabled': False,
            'legs': {'BTCUSDT': {'position': {'side': 'LONG', 'entry': 30_000}},
                     'ETHUSDT': {'position': None}},
            'risk_halt': 'TEST_HALT',
            'trends': {'BTCUSDT': {'wait_reason': 'WAIT_SAMPLE_SUPPORT'},
                       'ETHUSDT': {'wait_reason': 'WAIT_POSITIVE_NET_EDGE'}}})
        for value in ('BTCUSDT: 매수 / 진입가 30000', 'ETHUSDT: 보유 없음',
                      '원인 확인 필요 (서버 로그 참조)', '해당 조건의 과거 표본 부족', '비용 포함 기대값 기준 미달'):
            self.assertIn(value, text)

    def test_open_notification_uses_model_actual_probability_field_names(self):
        text = notify.render('OPEN', {'symbol': 'BTCUSDT', 'side': 'LONG',
                    'qty': .08, 'entry': 30_000, 'stop': 29_900, 'target': 30_200,
                    'initial_risk_usdt': 14,
                    'probability': {'n_group': 500, 'n_state': 100, 'p_mean': .7,
                                    'p05': .6, 'break_even': .4, 'lower95': .1}})
        self.assertIn('그룹 500 / 상태 100', text)
        self.assertIn('손익분기 0.4', text)
        self.assertIn('기대순R 하한 0.1', text)
        self.assertNotIn('None', text)

    def test_unsent_outbox_persists_failure_and_retries_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'outbox.sqlite'
            store = Store(path)
            with patch('basic_core.notify.now_ms', return_value=10_000):
                notify.enqueue(store, 'HALT', {'reason': 'OFFLINE_TEST'})
            failed_requests = []
            def offline(request, timeout):
                failed_requests.append((request, timeout))
                raise urllib.error.URLError('offline')
            box = notify.Outbox(path, {'token': 'dummy', 'chat': 'one-chat'}, opener=offline)
            self.assertFalse(box.step(store.db, now=10_000))
            row = store.db.execute('SELECT delivered,attempts,next_attempt FROM telegram_outbox').fetchone()
            self.assertEqual(row, (None, 1, 15_000))
            store.close()
            delivered_requests = []
            class Response:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self): return b'{"ok":true}'
            def online(request, timeout):
                delivered_requests.append((json.loads(request.data), timeout))
                return Response()
            db = sqlite3.connect(path)
            restarted = notify.Outbox(path, {'token': 'dummy', 'chat': 'one-chat'}, opener=online)
            self.assertFalse(restarted.step(db, now=14_999))
            self.assertEqual(delivered_requests, [])
            self.assertTrue(restarted.step(db, now=15_000))
            self.assertEqual(db.execute('SELECT delivered,attempts,last_error FROM telegram_outbox').fetchone(),
                             (15_000, 2, None))
            self.assertFalse(restarted.step(db, now=16_000))
            self.assertEqual(len(failed_requests), 1)
            self.assertEqual(len(delivered_requests), 1)
            self.assertEqual(delivered_requests[0][0]['chat_id'], 'one-chat')
            self.assertEqual(delivered_requests[0][1], 3)
            db.close()

    def test_unlisted_private_data_is_not_forwarded_by_notification_renderer(self):
        text = notify.render('HALT', {'reason': 'KNOWN_REASON',
                    'api_key': 'never-forward-this',
                    'private_snapshot': {'secret': 'never-forward-this-either'}})
        self.assertIn('원인 확인 필요 (서버 로그 참조)', text)
        self.assertNotIn('KNOWN_REASON', text)
        self.assertNotIn('never-forward-this', text)


class IndependentPendingRecoveryTests(unittest.TestCase):
    def machine(self, parent_missing=False, regression=False):
        class Memory:
            def __init__(self): self.values = {}; self.events = []
            def get(self, key, default=None): return copy.deepcopy(self.values.get(key, default))
            def set(self, key, value): self.values[key] = copy.deepcopy(value)
            def event(self, kind, value): self.events.append((kind, copy.deepcopy(value)))
        class Fake:
            def __init__(self): self.reads = []; self.writes = []
            def order(self, oid, cid):
                self.reads.append(cid)
                if cid == 'parent':
                    if parent_missing: raise DataError('parent details unavailable')
                    return {'symbol': 'BTCUSDT', 'orderId': 'parent-oid',
                            'clientOid': 'parent', 'category': CAT, 'side': 'buy',
                            'orderStatus': 'cancelled', 'cumExecQty': '0' if regression else '.008'}
                return {'symbol': 'BTCUSDT', 'orderId': 'child-oid',
                        'clientOid': 'child', 'side': 'sell',
                        'orderStatus': 'filled', 'cumExecQty': '.008'}
            def fills(self, oid):
                return [{'symbol': 'BTCUSDT', 'orderId': oid, 'execId': oid + '-fill',
                         'execQty': '.008', 'execPrice': '30000', 'execPnl': '0',
                         'feeDetail': [{'feeCoin': 'USDT', 'fee': '-.2'}],
                         'createdTime': '1577836820000'}]
            def post(self, path, body):
                self.writes.append((path, body)); raise AssertionError('no recovery write is authorized by this probe')
        c = json.loads((ROOT / 'config.json').read_text())
        store = Memory(); api = Fake(); live = Live(api, store, c, lambda: False, ROOT)
        live.instruments['BTCUSDT'] = Instrument('BTCUSDT', .1, .0001, .0001, 5, 10, .0006)
        parent = {'kind': 'ENTRY', 'cid': 'parent', 'oid': 'parent-oid',
                  'created': 1_577_836_800_000,
                  'body': {'symbol': 'BTCUSDT', 'side': 'buy', 'qty': '.02'},
                  'reported_cum_exec_qty': .008,
                  'safety_exit': {'cid': 'child', 'oid': 'child-oid',
                                  'body': {'symbol': 'BTCUSDT', 'side': 'sell', 'qty': '.008'}}}
        if regression: parent.pop('safety_exit')
        live.state['pending'] = parent; live.save()
        return live, api, store

    def test_child_exit_reconciles_even_when_parent_order_read_is_unavailable(self):
        original, api, store = self.machine(parent_missing=True)
        restarted = Live(api, store, original.c, lambda: False, ROOT)
        restarted.instruments = original.instruments
        restarted.reconcile_pending(1_577_836_820_000)
        pending = restarted.state['pending']
        self.assertEqual(api.reads, ['child', 'parent'])
        self.assertIsNone(pending['safety_exit'])
        self.assertAlmostEqual(pending['safety_closed_qty'], .008)
        self.assertEqual(pending['cid'], 'parent')
        self.assertEqual(api.writes, [])
        self.assertAlmostEqual(store.get('engine')['pending']['safety_closed_qty'], .008)

    def test_parent_cumulative_fill_regression_does_not_erase_durable_intent(self):
        live, api, store = self.machine(regression=True)
        with self.assertRaisesRegex(DataError, 'CUMULATIVE_FILL_REGRESSED'):
            live.reconcile_pending(1_577_836_820_000)
        self.assertEqual(live.state['pending']['cid'], 'parent')
        self.assertEqual(store.get('engine')['pending']['cid'], 'parent')
        self.assertAlmostEqual(live.state['pending']['reported_cum_exec_qty'], .008)
        self.assertEqual(api.writes, [])

    def test_child_wrong_known_id_or_category_is_never_adopted(self):
        for field, value in [('orderId', 'foreign-oid'), ('category', 'USDC-FUTURES')]:
            with self.subTest(field=field):
                live, api, store = self.machine(parent_missing=True)
                read = api.order
                def changed(oid, cid):
                    reply = read(oid, cid)
                    if cid == 'child': reply[field] = value
                    return reply
                api.order = changed
                with self.assertRaisesRegex(Exception, 'SAFETY_EXIT_.*MISMATCH'):
                    live.reconcile_pending(1_577_836_820_000)
                self.assertEqual(live.state['pending']['safety_exit']['cid'], 'child')
                self.assertEqual(store.get('engine')['pending']['safety_exit']['cid'], 'child')
                self.assertEqual(live.state['pending'].get('safety_closed_qty', 0), 0)
                self.assertEqual(api.writes, [])


class IndependentSettledFundingTests(unittest.TestCase):
    def test_latest_timestamped_settlement_ignores_future_and_other_symbol_rows(self):
        now = 1_577_836_800_000
        api = Rest()
        payload = {'resultList': [
            {'symbol': 'BTCUSDT', 'fundingRateTimestamp': str(now + 1), 'fundingRate': '.005'},
            {'symbol': 'BTCUSDT', 'fundingRateTimestamp': str(now - 3_600_000), 'fundingRate': '.0001'},
            {'symbol': 'ETHUSDT', 'fundingRateTimestamp': str(now), 'fundingRate': '.009'},
            {'symbol': 'BTCUSDT', 'fundingRateTimestamp': str(now - 1), 'fundingRate': '-.0002'}]}
        with patch.object(api, 'get', return_value=payload) as read:
            result = api.settled_funding('BTCUSDT', now)
        self.assertEqual(result['timestamp'], now - 1)
        self.assertEqual(result['rate'], -.0002)
        self.assertEqual(read.call_args[0][0], '/api/v3/market/history-fund-rate')
        self.assertEqual(read.call_args[0][1]['cursor'], '1')
        self.assertEqual(read.call_args[0][1]['category'], CAT)

    def test_missing_or_conflicting_settled_observation_fails_closed(self):
        now = 1_577_836_800_000
        payloads = [
            {'resultList': [{'symbol': 'BTCUSDT', 'fundingRateTimestamp': str(now + 1), 'fundingRate': '.0001'}]},
            {'resultList': [{'symbol': 'BTCUSDT', 'fundingTime': str(now), 'fundingRate': '.0001'}]},
            {'resultList': [{'symbol': 'BTCUSDT', 'fundingRateTimestamp': str(now), 'fundingRate': rate}
                            for rate in ('.0001', '.0002')]}]
        for payload in payloads:
            with self.subTest(payload=payload), patch.object(Rest, 'get', return_value=payload):
                with self.assertRaises(DataError): Rest().settled_funding('BTCUSDT', now)

    def test_settled_rate_cache_refreshes_at_next_five_minute_boundary(self):
        now = 1_577_836_800_000
        api = Rest()
        old = {'resultList': [{'symbol': 'BTCUSDT', 'fundingRateTimestamp': str(now - 60_000), 'fundingRate': '.0001'}]}
        new = {'resultList': [{'symbol': 'BTCUSDT', 'fundingRateTimestamp': str(now + 300_000), 'fundingRate': '-.0003'}]}
        with patch.object(api, 'get', side_effect=[old, new]) as read:
            before = api.settled_funding('BTCUSDT', now + 1)
            still_before = api.settled_funding('BTCUSDT', now + 299_999)
            after = api.settled_funding('BTCUSDT', now + 300_001)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(before['rate'], still_before['rate'])
        self.assertEqual(after['rate'], -.0003)


if __name__ == '__main__':
    unittest.main()
