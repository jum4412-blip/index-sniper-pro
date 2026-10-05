"""User-directed experimental mode does not relabel failed research as approval."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace
from cci_chop_v1 import cli, config, strategy, evidence
from cci_chop_v1._compat.core import SafetyError, Quote, now_ms
from cci_chop_v1.entry_market import MarketFrames


def model():
    return {'strategy_sha256':strategy.spec_sha256(),'entry_rule':strategy.ENTRY_RULE,
            'live_policy':evidence.AUTH_KIND,'measured_probability':None,
            'future_profitability_guaranteed':False,
            **{key:False for key in evidence.PROOF_FIELDS}}


class Experimental(unittest.TestCase):
    def test_gate_keeps_all_scientific_claims_false(self):
        evidence.experimental_check(model())
        gate=evidence.probability_gate(model(),'BTCUSDT','long',2,strategy.spec_sha256(),experimental=True)
        self.assertTrue(gate['eligible'])
        self.assertIsNone(gate['estimated_probability'])
        for key in ('deployment_approved','native_execution_verified','prospective_verified'):
            self.assertIs(gate[key],False)
        self.assertFalse(evidence.probability_gate(model(),'BTCUSDT','long',2,strategy.spec_sha256())['eligible'])

    def test_proof_flags_cannot_be_forged_to_experimentally_pass(self):
        for key in evidence.PROOF_FIELDS:
            for value in (True,1,'false',None):
                with self.assertRaises(SafetyError): evidence.experimental_check({**model(),key:value})
        with self.assertRaises(SafetyError): evidence.experimental_check({**model(),'measured_probability':.8})

    def test_arm_matches_requires_explicit_ack_kind(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); cli.state_dir(root).mkdir(parents=True)
            expected=cli.arm_payload(root,config.DEFAULT,'abc','123',True)
            path=cli.state_dir(root)/'ARM.json'; path.write_text(json.dumps(expected))
            self.assertTrue(cli.arm_matches(root,expected))
            path.write_text(json.dumps({**expected,'unvalidated_live_acknowledged':False}))
            self.assertFalse(cli.arm_matches(root,expected))
            path.write_text(json.dumps({**expected,'authorization_kind':'VERIFIED_MODEL'}))
            self.assertFalse(cli.arm_matches(root,expected))

    def test_default_config_constructs_live_engine(self):
        from cci_chop_v1.engine import CCEngine
        from cci_chop_v1._compat.store import Store
        with TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite')
            try: CCEngine(object(),store,{**config.validate(config.DEFAULT),'mode':'live'},lambda:False)
            finally: store.close()

    def test_market_execution_spread_validation_uses_quote_fields(self):
        at=now_ms(); q=Quote('BTCUSDT',at,100,99.99,100.01,100,100,0)
        api=SimpleNamespace(instrument=lambda _:object(),quote=lambda _:q,fee=lambda _:.0004)
        cli.validate_market_execution(api,config.DEFAULT)
        with self.assertRaises(SafetyError):
            cli.validate_market_execution(SimpleNamespace(instrument=api.instrument,quote=lambda _:Quote('BTCUSDT',at,100,99,101,100,100,0),fee=api.fee),config.DEFAULT)

    def test_selected_minute_frame_updates_between_five_minute_buckets(self):
        obj=MarketFrames(object()); calls=[]
        def recent(symbol,interval,width,decision,count):
            calls.append((interval,decision,count)); return [[decision//width*width-width,1,2,.5,1,3]]*count
        with patch('cci_chop_v1.entry_market.BaseMarketFrames.get_partial',return_value={'_errors':{},'H1':[]}):
            with patch('cci_chop_v1.entry_market.ENTRY_RULE',{'cci_frame':'M1','chop_frame':'M3'}),patch.object(obj,'_recent',side_effect=recent):
                obj.get_partial('BTCUSDT',1_800_000)
                obj.get_partial('BTCUSDT',1_860_000)
        self.assertEqual(len(calls),4)
        self.assertEqual({call[1] for call in calls},{1_800_000,1_860_000})
        self.assertIn(('1m',1_860_000,20),calls)
        self.assertIn(('3m',1_860_000,15),calls)

    def test_selected_filter_failure_preserves_structural_management_frames(self):
        obj=MarketFrames(object())
        base={'_errors':{},'H1':[[1]],'M5':[[2]],'tick_size':.1}
        with patch('cci_chop_v1.entry_market.BaseMarketFrames.get_partial',return_value=base),patch('cci_chop_v1.entry_market.ENTRY_RULE',{'cci_frame':'M1','chop_frame':'M1'}),patch.object(obj,'_recent',side_effect=SafetyError('CC_TEST_DATA_MISSING')):
            result=obj.get_partial('BTCUSDT',1_800_000)
        self.assertEqual(result['H1'],[[1]])
        self.assertEqual(result['M5'],[[2]])
        self.assertIn('M1',result['_errors'])


if __name__=='__main__': unittest.main()
