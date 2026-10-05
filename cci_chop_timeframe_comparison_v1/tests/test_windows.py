import csv
import json
import math
from pathlib import Path
import unittest

import numpy as np

from cci_chop_timeframe_comparison_v1 import runner


def candles(frame='M5',n=30):
    width=runner.WIDTHS[frame];origin=runner.replay.WEEK_ORIGIN if frame=='W' else 0
    p=np.linspace(100,160,n)
    return np.column_stack((origin+np.arange(n)*width,p,p+2,p-2,p+1,np.ones(n),p))


class WindowTests(unittest.TestCase):
    def test_predeclared_grid_is_complete_unique_and_fixed(self):
        specs=runner.variants()
        self.assertEqual(len(specs),144);self.assertEqual(len({s['id'] for s in specs}),144)
        pairs={(s['cci_frame'],s['chop_frame']) for s in specs if s['kind']=='combined'}
        self.assertEqual(pairs,{(a,b) for a in runner.FRAMES for b in runner.FRAMES})

    def test_complete_boundary_excludes_extreme_forming_or_future_candles(self):
        data=candles();width=runner.WIDTHS['M5']
        event={'entry_ms':20*width+runner.replay.MIN,'side':'long'}
        reference=runner.window_values(data,'M5',[event])
        mutated=data.copy();mutated[20:,1:5]+=1e9
        actual=runner.window_values(mutated,'M5',[event])
        self.assertEqual(reference['cci'][0],actual['cci'][0]);self.assertEqual(reference['chop'][0],actual['chop'][0])

    def test_all_frame_widths_use_exact_latest_complete_bar(self):
        for frame in runner.FRAMES:
            with self.subTest(frame=frame):
                data=candles(frame);width=runner.WIDTHS[frame]
                decision=int(data[19,0]+width)
                event={'entry_ms':decision+runner.replay.MIN,'side':'long'}
                good=runner.window_values(data,frame,[event])
                self.assertTrue(good['cci_valid'][0]);self.assertTrue(good['chop_valid'][0])
                stale=runner.window_values(data[:19],frame,[event])
                self.assertFalse(stale['cci_valid'][0]);self.assertFalse(stale['chop_valid'][0])

    def test_gap_in_indicator_lookback_denies_both_indicators(self):
        data=candles();width=runner.WIDTHS['M5']
        event={'entry_ms':20*width+runner.replay.MIN,'side':'long'}
        missing=np.delete(data,12,axis=0)
        result=runner.window_values(missing,'M5',[event])
        self.assertFalse(result['cci_valid'][0]);self.assertFalse(result['chop_valid'][0])

    def test_cci_matches_independent_scalar_formula(self):
        data=candles();width=runner.WIDTHS['M5'];event={'entry_ms':20*width+runner.replay.MIN}
        typical=[(float(r[2])+float(r[3])+float(r[4]))/3 for r in data[:20]]
        mean=sum(typical)/20;mad=sum(abs(x-mean) for x in typical)/20
        expected=(typical[-1]-mean)/(.015*mad)
        actual=runner.window_values(data,'M5',[event])['cci'][0]
        self.assertAlmostEqual(expected,float(actual),places=10)

    def test_chop_uses_previous_close_and_exact_last14_ranges(self):
        data=candles();data[5,4]=80;data[6:20,1:5]+=10
        width=runner.WIDTHS['M5'];event={'entry_ms':20*width+runner.replay.MIN}
        ranges=[max(float(data[k,2]-data[k,3]),abs(float(data[k,2]-data[k-1,4])),abs(float(data[k,3]-data[k-1,4]))) for k in range(6,20)]
        span=max(float(r[2]) for r in data[6:20])-min(float(r[3]) for r in data[6:20])
        expected=100*math.log10(sum(ranges)/span)/math.log10(14)
        actual=runner.window_values(data,'M5',[event])['chop'][0]
        self.assertAlmostEqual(expected,float(actual),places=10)

    def test_zero_range_is_unavailable(self):
        data=candles();data[:,1:5]=100;event={'entry_ms':20*runner.WIDTHS['M5']+runner.replay.MIN}
        result=runner.window_values(data,'M5',[event])
        self.assertFalse(result['cci_valid'][0]);self.assertFalse(result['chop_valid'][0])

    def test_side_threshold_and_cross_frame_and(self):
        pairs=[[{'side':'long'}],[{'side':'short'}],[{'side':'long'}]]
        features={'H6':{'cci':np.array([100,-100,150]),'cci_valid':np.ones(3,bool)},
                  'M3':{'chop':np.array([38.2,38.2,39]),'chop_valid':np.ones(3,bool)}}
        spec={'cci_frame':'H6','chop_frame':'M3'}
        mask,valid=runner.mask_for(spec,features,pairs)
        np.testing.assert_array_equal(mask,[True,True,False]);self.assertTrue(valid.all())


class ResultIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.results=Path(runner.__file__).parent/'results'
        self.summary=json.loads((self.results/'summary.json').read_text())

    def test_all_scenario_scope_rows_exist_and_keys_are_unique(self):
        with (self.results/'comparison_all.csv').open() as f:rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),3456)
        keys={(r['period'],r['variant'],r['fee'],r['cost'],r['scope']) for r in rows}
        self.assertEqual(len(keys),3456)
        self.assertEqual(sum(len(p['scenarios']) for p in self.summary['periods'].values()),1152)

    def test_selected_ledgers_reconcile_arithmetic_and_summary(self):
        chosen=self.summary['selected_rule']['id']
        for period,block in self.summary['periods'].items():
            for fee in ('personal','public'):
                for cost in (1,2):
                    with (self.results/f'{period}__{chosen}__{fee}__cost{cost}_trades.csv').open() as f:rows=list(csv.DictReader(f))
                    net=0
                    for row in rows:
                        sign=1 if row['side']=='long' else -1
                        gross=sign*(float(row['exit'])-float(row['entry']))*float(row['qty'])
                        self.assertAlmostEqual(float(row['gross_usdt']),gross,places=8)
                        expected=gross-float(row['fees_usdt'])+float(row['funding_usdt'])
                        self.assertAlmostEqual(float(row['net_usdt']),expected,places=8)
                        self.assertLessEqual(float(row['margin_usdt']),300+1e-8)
                        self.assertEqual(int(row['leverage']),5)
                        net+=expected
                    expected=block['scenarios'][f'{chosen}|{fee}|cost{cost}']['portfolio']['total_marked_net_usdt']
                    self.assertAlmostEqual(net,expected,places=7)

    def test_protocol_and_source_hashes_still_match(self):
        frozen=self.results/'frozen_protocol.json';protocol=json.loads(frozen.read_text())
        self.assertEqual(runner.base.sha(frozen),self.summary['protocol_sha256'])
        for relative,digest in protocol['source_code_sha256'].items():
            self.assertEqual(runner.base.sha(Path(runner.__file__).parent/relative),digest)

    def test_reported_bootstrap_quantiles_and_gate_claims_are_consistent(self):
        chosen=self.summary['selected_rule']['id']
        for period,block in self.summary['periods'].items():
            for fee in ('personal','public'):
                for cost in (1,2):
                    result=block['scenarios'][f'{chosen}|{fee}|cost{cost}']
                    for stat in [result['portfolio']]+list(result['by_symbol'].values()):
                        for unit in ('day','week'):
                            bounds=stat['bootstrap'][unit]
                            self.assertLessEqual(bounds['net_lower_familywise_usdt'],bounds['net_lower_5pct_usdt'])
                            self.assertLessEqual(bounds['incremental_lower_familywise_usdt'],bounds['incremental_lower_5pct_usdt'])
            for name,g in block['gates'].items():
                if g['passed']:self.assertEqual(g['failure_reasons'],[])
        self.assertEqual(self.summary['eligible_live_states'],0)
        self.assertIs(self.summary['deployment_approved'],False)


if __name__=='__main__':unittest.main()
