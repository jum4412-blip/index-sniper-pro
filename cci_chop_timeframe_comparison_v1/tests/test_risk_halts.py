import json
from pathlib import Path
import unittest
from unittest.mock import patch

from cci_chop_timeframe_comparison_v1 import risk_halt_supplement as supplement
from cci_chop_timeframe_comparison_v1.tests.test_replay import market,event,stub_materialize,MIN


class SupplementalRiskTests(unittest.TestCase):
    def test_daily_loss_denies_new_entry_and_records_persistent_halt(self):
        pools={'BTCUSDT':[event('BTCUSDT',1,2,'first',-25),event('BTCUSDT',4,5,'second',0)]}
        with patch.object(supplement.replay,'materialize',side_effect=stub_materialize):
            rows,rejected,halt,dd=supplement.portfolio({'BTCUSDT':market()},pools,1,True,0,40*MIN)
        self.assertEqual(len(rows),1);self.assertEqual(rejected['account_risk_limit'],1)
        self.assertIn('DAILY_LOSS_LIMIT',halt['reasons'])
        self.assertAlmostEqual(halt['equity_usdt'],supplement.replay.ACCOUNT_SNAPSHOT-25)

    def test_future_trade_profit_does_not_enter_current_equity(self):
        pools={'BTCUSDT':[event('BTCUSDT',1,20,'future',200)],'ETHUSDT':[event('ETHUSDT',2,10,'second',0)]}
        seen=[]
        def record(prepared,signal,path,leverage,multiplier,personal,equity):
            seen.append((signal['symbol'],equity))
            return stub_materialize(prepared,signal,path,leverage,multiplier,personal,equity)
        with patch.object(supplement.replay,'materialize',side_effect=record):
            rows,_,_,_=supplement.portfolio({'BTCUSDT':market(),'ETHUSDT':market('ETHUSDT')},pools,1,True,0,40*MIN)
        self.assertEqual(len(rows),2)
        self.assertAlmostEqual(seen[1][1],supplement.replay.ACCOUNT_SNAPSHOT-1)

    def test_risk_halt_keeps_managing_existing_schedule_and_never_auto_resumes(self):
        pools={'BTCUSDT':[event('BTCUSDT',1,2,'loss',-25),event('BTCUSDT',4,5,'blocked',0),event('BTCUSDT',15,16,'later',0)],
               'ETHUSDT':[event('ETHUSDT',1,10,'recovery',100)]}
        with patch.object(supplement.replay,'materialize',side_effect=stub_materialize):
            rows,rejected,halt,dd=supplement.portfolio({'BTCUSDT':market(),'ETHUSDT':market('ETHUSDT')},pools,1,True,0,40*MIN)
        self.assertEqual(len(rows),2);self.assertEqual(rejected['persistent_account_risk_halt'],1)
        self.assertIsNotNone(halt)
        self.assertEqual(sum(r['net_usdt'] for r in rows),75)

    def test_supplement_is_separately_frozen_and_does_not_reselect(self):
        here=Path(supplement.__file__).parent
        protocol=json.loads((here/'risk_halt_results'/'frozen_protocol.json').read_text())
        summary=json.loads((here/'risk_halt_results'/'summary.json').read_text())
        self.assertEqual(supplement.runner.base.sha(supplement.__file__),protocol['source_sha256'])
        self.assertEqual(len(summary['scenarios']),8)
        self.assertIs(summary['selection_changed'],False)
        for result in summary['scenarios'].values():
            self.assertIsNotNone(result['persistent_halt'])
            self.assertGreater(result['portfolio']['maximum_drawdown_1m_open_proxy'],.10)


if __name__=='__main__':unittest.main()
