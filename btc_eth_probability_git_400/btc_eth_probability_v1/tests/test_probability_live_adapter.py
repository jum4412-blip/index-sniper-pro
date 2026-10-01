"""Live/event parity, fixed size, outage protection; no external writes."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import probability_strategy as ps
from basic_core import strategy,account,runtime
from basic_core.core import Bar,Quote,Instrument,SafetyError,DataError,now_ms,fingerprint,atomic
from basic_core.engine import Live
from basic_core.api import Rest
from test_probability_execution import setup,pending,fill,exchange_position,config


def fixture():
    origin=1672531200000
    a=np.zeros((110,7));a[:,0]=origin+np.arange(110)*300000;a[:,1:5]=[30000,30030,29970,30000];a[:,5:]=[1,30000]
    a[-1,1:5]=[29985,30015,29850,30010];a[-1,5:]=[3,90030]
    event=ps.latest_event('BTCUSDT',a,now_ms=int(a[-1,0])+300000)
    assert event is not None
    times=1577836800000+np.arange(400)*86400000
    outcomes=np.where(np.arange(400)%5==0,-1.,1.5)
    labels=pd.DataFrame({'symbol':'BTCUSDT','entry_after_ms':times,'label_end_ms':times+60000,'net_r':outcomes,'label_profitable':outcomes>0,'event_type':event['event_type'],'side':event['side'],'state_key':event['state_key'],'group_key':event['group_key'],'structure_valid':True})
    model=ps.fit_probability_model(labels,int(times[-1])+2*86400000)
    model['label_notional_usdt']=2000
    bars=[Bar(int(row[0]),*row[1:]) for row in a]
    now=event['entry_after_ms']+1
    return a,event,model,bars,now


class LiveAdapterTests(unittest.TestCase):
    def test_legacy_label_sizing_cannot_arm_even_if_a_state_is_eligible(self):
        _,event,model,bars,now=fixture()
        q=Quote('BTCUSDT',now,30010,30009.7,30010.3,30010,30010,0)
        self.assertTrue(ps.predict_event(event,model)['eligible'])
        for legacy in (None,2500):
            old=copy.deepcopy(model)
            if legacy is None:old.pop('label_notional_usdt')
            else:old['label_notional_usdt']=legacy
            frame=strategy.frame('BTCUSDT',bars,config(),now,old,'historical')
            self.assertFalse(frame.probability['eligible'])
            self.assertEqual(frame.probability['reason'],'MODEL_LABEL_NOTIONAL_MISMATCH')
            self.assertIsNone(strategy.Crossings().observe(frame,q,config(),now))
            fake={'symbol':'BTCUSDT','side':'LONG','crossed':now,'trigger':event['entry_reference'],
                  'frame':frame.asdict()}
            with self.assertRaisesRegex(SafetyError,'PROBABILITY_EDGE_GATE_NOT_MET'):
                strategy.size(fake,q,Instrument('BTCUSDT',.1,.0001,.0001,5,10,.0006),config(),400,now)
    def test_exact_shared_completed_event_and_probability_lookup(self):
        a,event,model,bars,now=fixture();f=strategy.frame('BTCUSDT',bars,config(),now,model,'hash')
        self.assertEqual(f.event,event)
        self.assertEqual(f.probability,{**ps.predict_event(event,model),'label_notional_usdt':2000})
        mutated=a.copy();mutated[-1,1:5]=[1000,99999,100,2000]
        future=Bar(int(a[-1,0])+300000,30010,99999,1,2000,999999,1)
        self.assertEqual(strategy.frame('BTCUSDT',bars+[future],config(),now,model,'hash').event,event)
    def test_first_sample_after_one_minute_delay_is_event_not_invented_cross(self):
        _,event,model,bars,now=fixture();f=strategy.frame('BTCUSDT',bars,config(),now,model,'hash');q=Quote('BTCUSDT',now,30010,30009.7,30010.3,30010,30010,0)
        gate=strategy.Crossings()
        f=strategy.frame('BTCUSDT',bars,config(),event['entry_after_ms']-1,model,'hash')
        self.assertIsNone(gate.observe(f,q,config(),event['entry_after_ms']-1))
        sig=gate.observe(f,q,config(),now);self.assertIsNotNone(sig)
        self.assertEqual(sig['crossed'],event['entry_after_ms'])
        self.assertIsNone(gate.observe(f,Quote('BTCUSDT',now+30000,30010,30009.7,30010.3,30010,30010,0),config(),now+30000))
    def signal(self):
        _,event,model,bars,now=fixture();f=strategy.frame('BTCUSDT',bars,config(),now,model,'hash');q=Quote('BTCUSDT',now,30010,30009.7,30010.3,30010,30010,0)
        return strategy.Crossings().observe(f,q,config(),now),q,now
    def test_fixed_target_size_cost_budget_and_chase_rejections(self):
        sig,q,now=self.signal();inst=Instrument('BTCUSDT',.1,.0001,.0001,5,10,.0006)
        sized=strategy.size(sig,q,inst,config(),400,now)
        self.assertLessEqual(sized['notional'],2000);self.assertGreater(sized['notional'],1990);self.assertLessEqual(sized['estimated_risk'],20)
        self.assertAlmostEqual(sized['margin'],sized['notional']/5)
        bad=Quote('BTCUSDT',now,30045,30044.7,30045.3,30045,30045,0)
        with self.assertRaisesRegex(SafetyError,'CHASE_LIMIT'):strategy.size(sig,bad,inst,config(),500,now)
        inst=Instrument('BTCUSDT',.1,.0001,.0001,5,10,.0007)
        with self.assertRaisesRegex(SafetyError,'CURRENT_FEES'):strategy.size(sig,q,inst,config(),500,now)
    def test_probability_flag_cannot_bypass_numeric_support(self):
        sig,q,now=self.signal();sig['frame']['probability']['n_state']=79
        with self.assertRaisesRegex(SafetyError,'EDGE_GATE'):strategy.size(sig,q,Instrument('BTCUSDT',.1,.0001,.0001,5,10,.0006),config(),500,now)
    def test_fixed_target_and_time_manage_without_new_frame(self):
        p={'symbol':'BTCUSDT','side':'LONG','entry':30000,'initial_stop':29880,'stop':29880,'opened':0,'target':30240,'expires':3600000}
        q=Quote('BTCUSDT',1000,30241,30240,30242,30241,30241,0)
        self.assertEqual(strategy.manage(p,q,None,config(),1000),'FIXED_2R_TARGET')
        q=Quote('BTCUSDT',3600000,30000,29999,30001,30000,30000,0)
        self.assertEqual(strategy.manage(p,q,None,config(),3600000),'MAX_HOLDING_60_MINUTES')
    def test_executed_submitting_unknown_or_unspecified_protection_not_live(self):
        p={'symbol':'BTCUSDT','side':'LONG','initial_stop':29900,'price_step':.1,'qty_step':.0001,'qty':.02}
        row={'symbol':'BTCUSDT','posSide':'long','stopLoss':'29900','qty':'.02','slTriggerBy':'mark','slOrderType':'market','orderId':'sl','status':'pending'}
        self.assertEqual(account.protection([row],p),['sl'])
        for status in ('success','submitting','live','',None):
            row['status']=status;self.assertEqual(account.protection([row],p),[])
        row['status']='pending';row.pop('slOrderType');self.assertEqual(account.protection([row],p),[])


class PendingPartialProtectionTests(unittest.TestCase):
    def test_unknown_child_exit_survives_restart_without_duplicate_exit(self):
        live,api,store=setup();now=now_ms();p=pending(now-20000);p['reported_cum_exec_qty']=.008
        live.state['pending']=p;live.save();pos=exchange_position(p['created']);pos['total']='.008';api.position=[pos]
        api.unknown=True;live.protect_pending_partial(api.position,now)
        self.assertEqual(len(api.writes),1);self.assertEqual(api.writes[0][1]['reduceOnly'],'yes');self.assertEqual(api.writes[0][1]['qty'],'0.008')
        restarted=Live(api,store,config(),lambda:False,ROOT);restarted.instruments=live.instruments
        restarted.protect_pending_partial(api.position,now+1000)
        self.assertEqual(len(api.writes),1);self.assertEqual(restarted.state['pending']['safety_exit']['cid'],live.state['pending']['safety_exit']['cid'])
    def test_safety_child_reconciles_even_parent_order_read_unavailable(self):
        live,api,store=setup();now=now_ms();p=pending(now-20000);child={'cid':'PBS_test','oid':'safe','body':{'symbol':'BTCUSDT','side':'sell','qty':'.008'}};p['safety_exit']=child
        live.state['pending']=p;seen=[]
        def order(oid,cid):
            seen.append(cid)
            if cid=='PBS_test':return {'symbol':'BTCUSDT','clientOid':cid,'orderId':'safe','orderStatus':'filled','cumExecQty':'.008','side':'sell'}
            raise DataError('parent temporarily unavailable')
        api.order=order;api.fills=lambda oid:[{'symbol':'BTCUSDT','orderId':'safe','execId':'safe-fill','execQty':'.008','execPrice':'30000','execPnl':'0','feeDetail':[],'createdTime':str(now)}]
        live.reconcile_pending(now)
        self.assertEqual(seen[0],'PBS_test');self.assertEqual(p['safety_closed_qty'],.008);self.assertIsNone(p['safety_exit']);self.assertEqual(api.writes,[])
    def test_parent_cumulative_fill_regression_keeps_intent(self):
        live,api,store=setup();now=now_ms();p=pending(now);p['reported_cum_exec_qty']=.008;live.state['pending']=p
        api.info={'symbol':'BTCUSDT','orderId':'oid1','clientOid':p['cid'],'orderStatus':'cancelled','cumExecQty':'0'}
        with self.assertRaisesRegex(DataError,'FILL_REGRESSED'):live.reconcile_pending(now)
        self.assertIsNotNone(live.state['pending']);self.assertEqual(live.state['halt'],'ENTRY_CUMULATIVE_FILL_REGRESSED')

class SettledFundingTests(unittest.TestCase):
    def test_future_rate_cannot_replace_latest_settled_observation(self):
        api=Rest();now=1700000000000
        response={'resultList':[{'symbol':'BTCUSDT','fundingRateTimestamp':str(now-60000),'fundingRate':'.0001'},
                                {'symbol':'BTCUSDT','fundingRateTimestamp':str(now+60000),'fundingRate':'.001'}]}
        with patch.object(api,'get',return_value=response) as get:
            row=api.settled_funding('BTCUSDT',now)
            self.assertEqual(row['rate'],.0001);self.assertEqual(row['timestamp'],now-60000)
            self.assertEqual(get.call_args.args[1]['cursor'],'1')
    def test_missing_old_field_stale_nonfinite_conflicting_history_fails_closed(self):
        now=1700000000000
        bad=[[],{'resultList':[]},{'resultList':[{'symbol':'BTCUSDT','fundingTime':str(now),'fundingRate':0}]},
             {'resultList':[{'symbol':'BTCUSDT','fundingRateTimestamp':str(now-17*3600000),'fundingRate':0}]},
             {'resultList':[{'symbol':'BTCUSDT','fundingRateTimestamp':str(now),'fundingRate':'nan'}]},
             {'resultList':[{'symbol':'BTCUSDT','fundingRateTimestamp':str(now),'fundingRate':0}, {'symbol':'BTCUSDT','fundingRateTimestamp':str(now),'fundingRate':1}]}]
        for response in bad:
            with self.subTest(response=response),patch.object(Rest,'get',return_value=response),self.assertRaises(DataError):Rest().settled_funding('BTCUSDT',now)
    def test_cache_refreshes_at_five_minute_boundary(self):
        now=1700000100000//300000*300000
        api=Rest()
        with patch.object(api,'get',return_value={'resultList':[{'symbol':'BTCUSDT','fundingRateTimestamp':str(now),'fundingRate':0}]}) as get:
            api.settled_funding('BTCUSDT',now+1);api.settled_funding('BTCUSDT',now+100000)
            self.assertEqual(get.call_count,1)
            api.settled_funding('BTCUSDT',now+300000);self.assertEqual(get.call_count,2)


class RecoveryModelAndLegacyTests(unittest.TestCase):
    def test_missing_model_disarms_entries_but_frames_constructor_can_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'basic_core').mkdir();(root/'config.json').write_text('{}');(root/'probability_strategy.py').write_text('# synthetic')
            (root/'deployment_model.json').write_text('{}');digest=fingerprint(root);atomic(root/'data/LIVE_ENABLED.json',{'fingerprint':digest})
            (root/'deployment_model.json').unlink();self.assertFalse(runtime.authorized(root,digest))
            frames=runtime.Frames(config(),root,lambda *_:None,api=object())
            self.assertIsNone(frames.model);self.assertIsNone(frames.get('BTCUSDT'))
    def test_previous_eth_core_live_process_recognized_without_stopping_it(self):
        from basic_core.cli import processes
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc=root/'proc';row=proc/'9999999';row.mkdir(parents=True);old=root/'eth_larry_trail3_500';old.mkdir()
            (row/'cwd').symlink_to(old,target_is_directory=True)
            (row/'cmdline').write_bytes(b'python\0-B\0-m\0eth_core\0live\0');(row/'stat').write_text('9999999 (python) '+' '.join(['0']*22))
            found=processes(current_root=root/'new',proc_root=proc)
            self.assertEqual(found[0]['module'],'eth_core');self.assertEqual(found[0]['cwd'],str(old))
    def test_previous_trail3_armed_state_is_blocked_before_any_api_call(self):
        from basic_core.cli import legacy_state_guard
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);con={k:str(root/k) for k in ('env','legacy_root','trend_root','basic_root','psych_root','trail3_root')}
            (root/'connection.json').write_text(json.dumps(con));arm=Path(con['trail3_root'])/'data/LIVE_ENABLED.json';arm.parent.mkdir(parents=True);arm.write_text('{}')
            with self.assertRaisesRegex(SafetyError,'OLD_TRAIL3_ROOT_ARMED'):legacy_state_guard(root)

if __name__=='__main__':unittest.main()
