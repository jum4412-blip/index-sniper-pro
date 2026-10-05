import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import unittest
from unittest.mock import patch

from cci_chop_v1 import base_strategy as base
from cci_chop_v1 import strategy as s

NOW = int(datetime(2026, 10, 5, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)


def rows(frame, count=None, *, close=110.0, high=120.0, low=105.0):
    duration, anchor, limit, _ = base.FRAME_RULES[frame]
    end = (NOW - anchor) // duration * duration + anchor
    count = limit if count is None else count
    return [[end - (count - index) * duration, close, high, low, close, 100.0]
            for index in range(count)]


def structural_frames(side='long', *, trend_h1=False):
    result = {'W': rows('W', close=100, high=110, low=90),
              'D': rows('D', close=100, high=110, low=90),
              'H4': rows('H4'), 'H1': rows('H1'),
              'M5': rows('M5', close=111, high=113, low=109), 'tick_size': .1}
    for frame in ('W', 'D'):
        result[frame][-1] = [result[frame][-1][0], 100, 116, 95, 115, 100]
    result['H4'][110][3] = 90
    result['H1'][61][3] = 95
    result['H1'][67][3] = 100
    if trend_h1:
        result['H1'] = [[row[0], 70 + .6*i, 70 + .6*i + .2,
                         70 + .6*i - .2, 70 + .6*i, 100]
                        for i, row in enumerate(result['H1'])]
        result['H1'][61][3] = 105.1
        result['H1'][67][3] = 108.7
    result['M5'][-1] = [result['M5'][-1][0], 111, 116, 110, 115, 200]
    if side == 'short':
        for frame in base.FRAME_RULES:
            result[frame] = [[ts, 220-op, 220-low, 220-high, 220-close, volume]
                             for ts, op, high, low, close, volume in result[frame]]
    return result


def trend_rows(frame, count=24, side='long'):
    duration, anchor = s.INDICATOR_FRAMES[frame]
    end = (NOW-anchor) // duration * duration + anchor
    result = []
    for i in range(count):
        price = 100.0+i if side == 'long' else 200.0-i
        result.append([end-(count-i)*duration, price, price+.2, price-.2, price, 100])
    return result


class CciChopStrategyTests(unittest.TestCase):
    def test_spec_freezes_joint_filter_and_is_distinct_from_base(self):
        self.assertNotEqual(s.spec_sha256(), base.spec_sha256())
        self.assertEqual(s.BASE_SPEC_SHA256, base.spec_sha256())
        filters = s.SPEC['supplemental_entry_filters']
        self.assertEqual(filters['cci_frame'], s.ENTRY_RULE['cci_frame'])
        self.assertEqual(filters['chop_frame'], s.ENTRY_RULE['chop_frame'])
        self.assertTrue(filters['entry_only'])
        self.assertFalse(filters['indicator_reversal_is_exit'])
        self.assertFalse(s.SPEC['individual_indicator_results_are_joint_results'])
        self.assertEqual(base.SPEC['name'], 'W_D_H4_H1_M5_STRUCTURAL_TREND_V1')

    def test_cci_reference_linear_and_inverse_with_same_window_mad(self):
        for side, expected in (('long', 126.66666666666667), ('short', -126.66666666666667)):
            sample = trend_rows('H1', 20, side)
            bars = s.completed_candles(sample, NOW, s.HOUR_MS)
            self.assertAlmostEqual(s.cci20(bars), expected, places=10)

    def test_cci_flat_and_insufficient_window_are_unavailable(self):
        self.assertIsNone(s.cci20(s.completed_candles(rows('H1', 20), NOW, s.HOUR_MS)))
        self.assertIsNone(s.cci20(s.completed_candles(trend_rows('H1', 19), NOW, s.HOUR_MS)))

    def test_chop_reference_requires_previous_close_and_excludes_previous_extrema(self):
        data = [[0, 50, 50, 50, 50, 1]]
        data.extend([[i*s.HOUR_MS, 99.5+i, 100+i, 99+i, 99.5+i, 1]
                     for i in range(1, 15)])
        bars = s.completed_candles(data, 15*s.HOUR_MS, s.HOUR_MS)
        expected = 100*math.log10(70.5/14)/math.log10(14)
        self.assertAlmostEqual(s.chop14(bars), expected, places=11)
        self.assertGreater(s.chop14(bars), 38.2)
        self.assertIsNone(s.chop14(bars[1:]))
        self.assertLess(100*math.log10(20.5/14)/math.log10(14), 38.2)

    def test_chop_zero_range_and_gap_above_100_not_clamped(self):
        point = [[i*s.HOUR_MS, 100, 100, 100, 100, 1] for i in range(15)]
        self.assertIsNone(s.chop14(s.completed_candles(point, 15*s.HOUR_MS, s.HOUR_MS)))
        gap = [[0, 1, 1, 1, 1, 1]]
        gap.extend([[i*s.HOUR_MS, 100, 101, 100, 100, 1] for i in range(1, 15)])
        self.assertGreater(s.chop14(s.completed_candles(gap, 15*s.HOUR_MS, s.HOUR_MS)), 100)

    def test_joint_h1_entry_preserves_base_fields_in_both_directions(self):
        with patch.dict(s.ENTRY_RULE, cci_frame='H1', chop_frame='H1'):
            for side in ('long', 'short'):
                sample = structural_frames(side, trend_h1=True)
                signal = s.generate_signal('BTCUSDT', sample, NOW)
                baseline = base.generate_signal('BTCUSDT', sample, NOW)
                self.assertIsNotNone(signal)
                self.assertEqual(signal.side, side)
                self.assertEqual((signal.guard, signal.closed_ms, signal.breakout_level, signal.score),
                                 (baseline.guard, baseline.closed_ms, baseline.breakout_level, baseline.score))
                self.assertTrue(signal.metadata['indicators'][side])
                self.assertEqual(signal.metadata['indicators']['cci_closed_ms'], NOW//s.HOUR_MS*s.HOUR_MS)
                self.assertEqual(signal.metadata['spec_sha256'], s.spec_sha256())
                self.assertIsNone(signal.metadata['probability'])
                identity = f'{s.spec_sha256()}|BTCUSDT|{side}|{NOW}'
                self.assertEqual(signal.event_id, hashlib.sha256(identity.encode()).hexdigest())
                self.assertNotEqual(signal.event_id, baseline.event_id)

    def test_and_not_or_chop_fail_and_direction_fail_independently(self):
        with patch.dict(s.ENTRY_RULE, cci_frame='H1', chop_frame='H1'):
            sample = structural_frames(trend_h1=True)
            good = s.indicator_evidence(sample, NOW)
            for changes, reason in (({'chop14': 38.20001, 'long': False}, 'CHOP14_NOT_TRENDING'),
                                    ({'cci20': 99.99999, 'long': False}, 'CCI20_LONG_NOT_CONFIRMED')):
                with patch.object(s, 'indicator_evidence', return_value={**good, **changes}):
                    self.assertIsNone(s.generate_signal('BTCUSDT', sample, NOW))
                    self.assertIn(reason, s.analyze(sample, NOW)['wait_reasons'])
            short = structural_frames('short', trend_h1=True)
            short_evidence = s.indicator_evidence(short, NOW)
            with patch.object(s, 'indicator_evidence', return_value={**short_evidence, 'cci20': -99.99,
                                                                     'short': False}):
                self.assertIn('CCI20_SHORT_NOT_CONFIRMED', s.analyze(short, NOW)['wait_reasons'])

    def test_inclusive_thresholds_are_not_strict_breakouts(self):
        with patch.dict(s.ENTRY_RULE, cci_frame='H1', chop_frame='H1'):
            for side, cci in (('long', 100.0), ('short', -100.0)):
                sample = structural_frames(side, trend_h1=True)
                with patch.object(s, 'cci20', return_value=cci), patch.object(s, 'chop14', return_value=38.2):
                    self.assertTrue(s.indicator_evidence(sample, NOW)[side])
                    self.assertIsNotNone(s.generate_signal('BTCUSDT', sample, NOW))

    def test_base_entry_regime_remains_required_even_when_filters_pass(self):
        sample = structural_frames(trend_h1=True)
        sample['D'] = structural_frames('short')['D']
        with patch.dict(s.ENTRY_RULE, cci_frame='H1', chop_frame='H1'):
            self.assertTrue(s.indicator_evidence(sample, NOW)['long'])
            self.assertIsNone(s.generate_signal('BTCUSDT', sample, NOW))
            self.assertIn('W_D_DIRECTION_CONFLICT', s.analyze(sample, NOW)['wait_reasons'])

    def test_selected_frames_can_differ_weekly_cci_and_minute_chop(self):
        sample = {'W': trend_rows('W'), 'M1': trend_rows('M1')}
        with patch.dict(s.ENTRY_RULE, cci_frame='W', chop_frame='M1'):
            evidence = s.indicator_evidence(sample, NOW)
            self.assertTrue(evidence['long'])
            self.assertEqual(evidence['cci_closed_ms'], (NOW-s.WEEK_ANCHOR_MS)//s.WEEK_MS*s.WEEK_MS+s.WEEK_ANCHOR_MS)
            self.assertEqual(evidence['chop_closed_ms'], NOW//60_000*60_000)
            self.assertIsNone(evidence['closed_ms'])
            self.assertEqual(evidence['cci_frame'], 'W')
            self.assertEqual(evidence['chop_frame'], 'M1')

    def test_all_eleven_frame_alignment_and_selected_half_hour(self):
        for frame in s.INDICATOR_FRAMES:
            with self.subTest(frame=frame), patch.dict(s.ENTRY_RULE, cci_frame=frame, chop_frame=frame):
                sample = {frame: trend_rows(frame)}
                evidence = s.indicator_evidence(sample, NOW)
                self.assertTrue(evidence['long'])
                duration, anchor = s.INDICATOR_FRAMES[frame]
                self.assertEqual(evidence['closed_ms'], (NOW-anchor)//duration*duration+anchor)
        self.assertEqual(len(s.INDICATOR_FRAMES), 11)

    def test_forming_selected_frame_prices_ignored_before_parsing(self):
        for frame in ('W', 'H1', 'M30', 'M1'):
            duration, anchor = s.INDICATOR_FRAMES[frame]
            with self.subTest(frame=frame), patch.dict(s.ENTRY_RULE, cci_frame=frame, chop_frame=frame):
                sample = {frame: trend_rows(frame)}
                original = s.indicator_evidence(sample, NOW)
                forming_open = (NOW-anchor)//duration*duration+anchor
                sample[frame].append([forming_open, None, math.nan, -1, object(), 'bad'])
                self.assertEqual(s.indicator_evidence(sample, NOW), original)
                self.assertFalse(s.indicator_evidence(sample, forming_open+duration)['valid'])

    def test_closed_gap_duplicate_stale_and_alignment_block_selected_frame(self):
        for frame in ('W', 'M30', 'M1'):
            for mutation in ('gap', 'duplicate', 'stale', 'misaligned'):
                with self.subTest(frame=frame, mutation=mutation), patch.dict(s.ENTRY_RULE, cci_frame=frame, chop_frame=frame):
                    sample = {frame: trend_rows(frame)}
                    if mutation == 'gap':
                        sample[frame].pop(-3)
                    elif mutation == 'duplicate':
                        sample[frame].append(copy.deepcopy(sample[frame][-1]))
                    elif mutation == 'stale':
                        sample[frame].pop()
                    else:
                        sample[frame][-1][0] += 1
                    evidence = s.indicator_evidence(sample, NOW)
                    self.assertFalse(evidence['valid'])
                    self.assertFalse(evidence['long'])
                    self.assertEqual(evidence['reason'], 'CCI_CHOP_SELECTED_FRAME_DATA_UNAVAILABLE')

    def test_flat_selected_frame_blocks_entry_without_zero_probability(self):
        with patch.dict(s.ENTRY_RULE, cci_frame='H1', chop_frame='H1'):
            sample = structural_frames()
            sample['H1'][61][2] = 130  # Keep TP constant despite the structural low.
            sample['H1'][67][2] = 125
            self.assertIsNotNone(base.generate_signal('BTCUSDT', sample, NOW))
            self.assertIsNone(s.generate_signal('BTCUSDT', sample, NOW))
            self.assertIn('CCI_CHOP_DEGENERATE_SELECTED_FRAME', s.analyze(sample, NOW)['wait_reasons'])

    def test_indicator_failure_does_not_exit_or_loosen_protection(self):
        with patch.dict(s.ENTRY_RULE, cci_frame='M30', chop_frame='M1'):
            for side, guard in (('long', 98), ('short', 122), ('long', 101), ('short', 119)):
                sample = structural_frames(side)
                expected = base.update_guard(side, guard, sample, NOW)
                actual = s.update_guard(side, guard, sample, NOW)
                self.assertEqual((actual.guard, actual.candidate_guard, actual.should_exit, actual.reason),
                                 (expected.guard, expected.candidate_guard, expected.should_exit, expected.reason))
                self.assertFalse(actual.metadata['entry_filters_apply_to_exit'])
                self.assertEqual(actual.metadata['spec_sha256'], s.spec_sha256())
                self.assertEqual(actual.guard, max(guard, actual.guard) if side == 'long' else min(guard, actual.guard))

    def test_context_macro_and_guards_unchanged_when_selected_data_missing(self):
        sample = structural_frames()
        expected = base.context(sample, NOW)
        with patch.dict(s.ENTRY_RULE, cci_frame='M30', chop_frame='M1'):
            actual = s.context(sample, NOW)
        for key in ('direction', 'side', 'native_guards', 'h1_guards', 'structure_valid', 'frame_errors'):
            self.assertEqual(actual[key], expected[key])
        self.assertFalse(actual['indicators']['valid'])
        self.assertFalse(actual['entry_filters_apply_to_exit'])

    def test_event_identity_is_same_for_repeated_reads_and_changes_for_next_m5(self):
        sample = structural_frames(trend_h1=True)
        with patch.dict(s.ENTRY_RULE, cci_frame='H1', chop_frame='H1'):
            first = s.generate_signal('ETHUSDT', sample, NOW)
            same = s.generate_signal('ETHUSDT', sample, NOW+30_000)
            self.assertEqual(first.event_id, same.event_id)
            sample['M5'].append([NOW, 115, 121, 114, 120, 200])
            following = s.generate_signal('ETHUSDT', sample, NOW+s.M5_MS)
            self.assertNotEqual(first.event_id, following.event_id)
            self.assertEqual(following.closed_ms, NOW+s.M5_MS)

    def test_rule_rejects_unsupported_frames_changed_thresholds_and_duplicate_keys(self):
        rule = {'cci_frame': 'H1', 'chop_frame': 'H1', 'cci_period': 20,
                'cci_long': 100, 'cci_short': -100, 'chop_period': 14, 'chop_max': 38.2}
        for invalid in ({**rule, 'cci_frame': 'H2'}, {**rule, 'chop_max': 50},
                        {**rule, 'cci_period': True}, {**rule, 'cci_short': 100},
                        {**rule, 'chop_frame': None}, ['not a dictionary']):
            with self.subTest(rule=invalid), patch('pathlib.Path.read_text', return_value=json.dumps(invalid)):
                with self.assertRaises(s.StrategyDataError):
                    s._read_entry_rule()
        duplicate = json.dumps(rule)[:-1]+',"cci_frame":"M1"}'
        with patch('pathlib.Path.read_text', return_value=duplicate):
            with self.assertRaises(s.StrategyDataError):
                s._read_entry_rule()
        with patch('pathlib.Path.read_text', return_value=json.dumps(rule)):
            self.assertEqual(s._read_entry_rule(), rule)

    def test_file_rule_is_read_once_not_each_signal(self):
        with patch('pathlib.Path.read_text', side_effect=AssertionError('Runtime rule reload')):
            with patch.dict(s.ENTRY_RULE, cci_frame='H1', chop_frame='H1'):
                self.assertIsNotNone(s.generate_signal('BTCUSDT', structural_frames(trend_h1=True), NOW))

    def test_missing_selected_frame_exposes_error_and_unsupported_symbol_refused(self):
        with patch.dict(s.ENTRY_RULE, cci_frame='M30', chop_frame='M1'):
            diagnostic = s.analyze(structural_frames(), NOW)
            self.assertFalse(diagnostic['eligible'])
            self.assertIn('CCI_CHOP_SELECTED_FRAME_DATA_UNAVAILABLE', diagnostic['wait_reasons'])
        with self.assertRaises(s.StrategyDataError):
            s.generate_signal('DOGEUSDT', structural_frames(), NOW)


if __name__ == '__main__':
    unittest.main()
