"""Offline tests. No network, API credentials, or actual orders."""
import copy,json,random,tempfile,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal
from trend_core.core import *
from trend_core import strategy,risk,account,runtime
from trend_core.api import Rest,fill_summary,rows
from trend_core.engine import Live,Paper
from trend_core.store import Store

ROOT=Path(__file__).resolve().parents[1]
END=20_000*DAY
NOW=END+60_000

def conf():return config(ROOT)
def quote(symbol='ETHUSDT',price=3000.,stamp=NOW):
    delta=price*.00002
    return Quote(symbol,stamp,price,price-delta,price+delta,price,price,0.)
def instrument(symbol='ETHUSDT'):
    return Instrument(symbol,.01 if symbol=='ETHUSDT' else .1,.001 if symbol=='ETHUSDT' else .0001,
                      .001 if symbol=='ETHUSDT' else .0001,5.,100.,.0006)
def sig(symbol='ETHUSDT',side='LONG',price=3000.,key='sample'):
    atr=price*.01
    return {'key':key,'symbol':symbol,'side':side,'trigger':price*(.999 if side=='LONG' else 1.001),'bar_end':END,
            'frame':{'atr':atr,'close':price,'initial_low':price*.98,'initial_high':price*1.02,'built':NOW}}
def position(side='LONG',hold='one_way_mode'):
    s=sig(side=side);i=instrument();z=strategy.size(s,quote(),i,conf(),1018,NOW)
    return {'id':'owned','symbol':'ETHUSDT','side':side,'opened':NOW-20_000,'entry':3000.,
            'qty':z['qty'],'original_qty':z['qty'],'initial_stop':z['stop'],'stop':z['stop'],
            'initial_r':abs(3000-z['stop']),'price_step':.01,'qty_step':.001,'entry_oid':'opening-order',
            'entry_fee':.1,'entry_equity':1018.,'fee_rate':.0006,'risk_budget':5.09,'hold_mode':hold,
            'protect_deadline':NOW-1000,'config':conf(),'mfe_r':0.,'mae_r':0.,'rejected_exits':0}
def exchange_position(p):
    return {'symbol':p['symbol'],'category':CAT,'posSide':p['side'].lower(),'total':str(p['qty']),
            'avgPrice':str(p['entry']),'marginMode':'crossed','leverage':'5','createdTime':str(p['opened'])}
def protective_order(p):
    return {'symbol':p['symbol'],'category':CAT,'posSide':p['side'].lower(),'status':'live','qty':str(p['qty']),
            'stopLoss':str(p['initial_stop']),'slTriggerBy':'mark','slOrderType':'market','orderId':'stop-1'}
def history(p,net=-2.):
    funding=-.03;openfee=-.12;closefee=-.11;gross=net-funding-openfee-closefee
    exit_price=p['entry']+gross/p['original_qty']*(1 if p['side']=='LONG' else -1)
    return {'symbol':p['symbol'],'posSide':p['side'].lower(),'createdTime':str(p['opened']),
            'updatedTime':str(NOW),'positionId':'history-owned','openTotalPos':str(p['original_qty']),
            'closeTotalPos':str(p['original_qty']),'openPriceAvg':str(p['entry']),'closePriceAvg':str(exit_price),
            'cumRealisedPnl':str(gross),'totalFunding':str(funding),'openFeeTotal':str(openfee),
            'closeFeeTotal':str(closefee),'netProfit':str(net)}

class FakeAPI:
    def __init__(self):
        self.sent=[];self.pos=[];self.stops=[];self.historical=[];self.fail=None;self.before_post=None
        self.order_data={};self.fill_data={};self.equity=1018.;self.flat_checks=0
        self.setting={'accountMode':'unified','holdMode':'one_way_mode',
                     'symbolConfigList':[{'category':CAT,'symbol':s,'marginMode':'crossed','leverage':'5'} for s in SYMBOLS]}
    def instrument(self,s):return instrument(s)
    def quote(self,s):return quote(s,3000. if s=='ETHUSDT' else 100000.)
    def positions(self,category=CAT):return copy.deepcopy(self.pos) if category==CAT else []
    def assets(self):return {'usdtEquity':str(self.equity)}
    def settings(self):return copy.deepcopy(self.setting)
    def open_orders(self,category=CAT):return []
    def strategies(self,category=CAT,kind='tpsl'):return copy.deepcopy(self.stops) if category==CAT and kind=='tpsl' else []
    def get(self,path,params=None,private=False):
        if path=='/api/v3/account/info':return {'userId':'test-user','permissions':['uta_trade','uta_mgt']}
        raise AssertionError('unexpected API read '+path)
    def pages(self,path,params):
        if path=='/api/v3/position/history-position':return copy.deepcopy(self.historical)
        raise AssertionError('unexpected paginated read '+path)
    def post(self,path,body):
        if self.before_post:self.before_post(path,body)
        self.sent.append((path,copy.deepcopy(body)))
        if self.fail:raise self.fail
        return {'orderId':f'o{len(self.sent)}','clientOid':body.get('clientOid','')}
    def order(self,oid='',cid=''):
        if not self.order_data:raise Rejected('order not available yet')
        return copy.deepcopy(self.order_data)
    def fills(self,oid):return copy.deepcopy(self.fill_data.get(oid,[]))
    def filled(self,pending,qty,price,status='filled'):
        oid=pending.get('oid','o1')
        self.order_data={'orderId':oid,'clientOid':pending['cid'],'symbol':pending['body']['symbol'],
                         'orderStatus':status,'cumExecQty':str(qty)}
        self.fill_data[oid]=[{'execId':oid+'-f','orderId':oid,'symbol':pending['body']['symbol'],
                             'side':pending['body']['side'],'tradeSide':'open' if pending['kind']=='ENTRY' else 'close',
                             'execQty':str(qty),'execPrice':str(price),'execPnl':'0',
                             'feeDetail':[{'feeCoin':'USDT','fee':str(qty*price*.0006)}],'createdTime':str(NOW)}]

class StrategyTests(unittest.TestCase):
    def test_sizing_never_exceeds_budget_or_margin_for_both_sides(self):
        rng=random.Random(45);count=0
        for _ in range(350):
            symbol=rng.choice(SYMBOLS);price=rng.uniform(2000,5000) if symbol=='ETHUSDT' else rng.uniform(40000,150000)
            side=rng.choice(['LONG','SHORT']);equity=rng.uniform(500,10000)
            z=strategy.size(sig(symbol,side,price),quote(symbol,price),instrument(symbol),conf(),equity,NOW)
            self.assertLessEqual(z['estimated_risk'],equity*.005+1e-9)
            self.assertLessEqual(z['margin'],equity*.30+1e-9)
            self.assertEqual(Decimal(str(z['qty']))%Decimal(str(instrument(symbol).qty_step)),0)
            self.assertGreater(z['qty'],0);count+=1
        self.assertEqual(count,350)
    def test_minimum_order_is_skipped_not_rounded_up(self):
        with self.assertRaisesRegex(SafetyError,'MINIMUM'):
            strategy.size(sig(),quote(),instrument(),conf(),.50,NOW)
    def test_bad_quotes_and_execution_conditions_block(self):
        for q in (quote(stamp=NOW-9000),Quote('ETHUSDT',NOW,3000,2990,3010,3000,3000,0),
                  Quote('ETHUSDT',NOW,3000,2999.95,3000.05,3020,3000,0),
                  Quote('ETHUSDT',NOW,3000,2999.95,3000.05,3000,3000,.01)):
            with self.subTest(q=q),self.assertRaises(SafetyError):strategy.size(sig(),q,instrument(),conf(),1018,NOW)
    def test_future_and_expired_signals_block(self):
        for end in (NOW+H4,NOW-901_000):
            s=sig();s['bar_end']=end
            with self.assertRaises(SafetyError):strategy.size(s,quote(),instrument(),conf(),1018,NOW)
    def test_cost_filter_blocks_tiny_stops(self):
        s=sig();s['frame']['atr']=2;s['frame']['initial_low']=2990
        with self.assertRaises(SafetyError):strategy.size(s,quote(),instrument(),conf(),1018,NOW)
    def test_completed_bars_ignore_future_and_current_candle(self):
        d=[Bar(END-(200-i)*DAY,100+i*.1,101+i*.1,99+i*.1,100+i*.1,10,1000) for i in range(200)]
        h=[Bar(END-(100-i)*H4,100+i*.1,101+i*.1,99+i*.1,100+i*.1,10,1000) for i in range(100)]
        h[-1]=Bar(END-H4,110,114,109,113,10,1100)
        before=strategy.frame('ETHUSDT',d,h,conf(),NOW)
        after=strategy.frame('ETHUSDT',d+[Bar(END,1,9999,1,9999,1,1)],h+[Bar(END,1,9999,1,9999,1,1)],conf(),NOW)
        self.assertEqual(before,after);self.assertEqual(before.direction,1)
        s=strategy.signal(before,conf(),NOW);self.assertIsNotNone(s);self.assertEqual(s['side'],'LONG')
        self.assertIsNone(strategy.signal(before,conf(),END+901_000))
    def test_candle_gaps_are_rejected(self):
        b=[Bar(i*H4,1,2,1,1,1,1) for i in (1,2,4)]
        with self.assertRaisesRegex(DataError,'gap'):strategy.completed(b,H4,6*H4,3)
    def test_exchange_daily_session_offset_is_respected(self):
        offset=16*3_600_000
        b=[Bar(i*DAY+offset,1,2,1,1,1,1) for i in range(4)]
        result=strategy.completed(b,DAY,4*DAY+offset+6000,4)
        self.assertEqual(result[-1].ts,3*DAY+offset)
        result=strategy.completed(b,DAY,4*DAY+offset-1000,3)
        self.assertEqual(result[-1].ts,2*DAY+offset)
    def test_missing_latest_daily_bar_cannot_supply_old_direction(self):
        offset=16*3_600_000
        b=[Bar(i*DAY+offset,1,2,1,1,1,1) for i in range(3)]
        with self.assertRaisesRegex(DataError,'latest completed'):
            strategy.completed(b,DAY,4*DAY+offset+60_000,3)
    def test_trailing_never_loosens_or_jumps_through_market(self):
        for side in ('LONG','SHORT'):
            p=position(side);sign=1 if side=='LONG' else -1
            px=p['entry']+sign*p['initial_r']*1.05;q=quote(price=px)
            f=strategy.Frame('ETHUSDT',NOW,NOW-10_000,END,sign,30,0,0,px,px,0,0,2900,3100,2950,3050)
            old=p['stop'];reason=strategy.manage(p,q,f,conf(),NOW)
            self.assertIsNone(reason);self.assertGreaterEqual(sign*(p['stop']-old),0)
            self.assertGreater(sign*(q.mark-p['stop']),0)
            looser=strategy.Frame('ETHUSDT',NOW,NOW-5000,END,sign,30,0,0,px,px,0,0,2800,3200,2800,3200)
            old=p['stop'];strategy.manage(p,q,looser,conf(),NOW);self.assertEqual(p['stop'],old)
    def test_new_channel_already_crossed_exits_without_installing_wrong_stop(self):
        p=position();q=quote(price=3000);old=p['stop']
        f=strategy.Frame('ETHUSDT',NOW,NOW-10_000,END,1,30,0,0,3000,3000,0,0,2990,3050,3020,3050)
        self.assertEqual(strategy.manage(p,q,f,conf(),NOW),'CHANNEL_EXIT');self.assertEqual(p['stop'],old)

class RiskTests(unittest.TestCase):
    def test_guard_latches_until_day_roll_not_equity_rebound(self):
        s=risk.fresh();risk.blocks(s,1000,conf(),NOW)
        self.assertIn('DAILY_LOSS_LIMIT',risk.blocks(s,979,conf(),NOW+1000))
        self.assertIn('DAILY_LOSS_LIMIT',risk.blocks(s,1005,conf(),NOW+2000))
        self.assertNotIn('DAILY_LOSS_LIMIT',risk.blocks(s,1005,conf(),NOW+DAY))
    def test_legacy_losses_and_pause_survive_install(self):
        old=risk.fresh();risk.blocks(old,1040,conf(),NOW)
        old['loss_pause_until']=NOW+DAY;old['consecutive_losses']=3
        s=risk.fresh();risk.inherit(s,old)
        b=risk.blocks(s,1010,conf(),NOW)
        self.assertIn('DAILY_LOSS_LIMIT',b);self.assertIn('CONSECUTIVE_LOSS_PAUSE',b)
        self.assertEqual(s['day_start_equity'],1040);self.assertEqual(s['peak_equity'],1040)
    def test_drawdown_does_not_reset_on_new_day(self):
        s=risk.fresh();risk.blocks(s,1000,conf(),NOW)
        self.assertIn('MAX_DRAWDOWN',risk.blocks(s,919,conf(),NOW+DAY))
        self.assertIn('MAX_DRAWDOWN',risk.blocks(s,1001,conf(),NOW+2*DAY))
    def test_loss_counter_uses_net_not_gross(self):
        s=risk.fresh()
        for k in range(3):risk.closed(s,-.01,conf(),NOW+k)
        self.assertGreater(s['loss_pause_until'],NOW+DAY-1)
        risk.closed(s,.01,conf(),NOW+DAY);self.assertEqual(s['consecutive_losses'],0)
    def test_legacy_reader_is_read_only(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);p=root/'data/larry_v2/live.sqlite';store=Store(p)
            store.set('live',{'position':{'x':1}});store.set('account_uid','abc');store.close()
            state,uid=risk.old_state(root);self.assertEqual(uid,'abc')
            with self.assertRaisesRegex(SafetyError,'POSITION_OR_PENDING'):risk.legacy_check(root)

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'live.sqlite');self.api=FakeAPI();self.c=conf()
        self.clock=patch('trend_core.engine.now_ms',lambda:NOW);self.clock.start()
        self.proc=patch('trend_core.cli.legacy_process_guard',lambda:None);self.proc.start()
        self.b=Live(self.api,self.store,self.c,lambda:True,self.root/'old');self.b.boot()
    def tearDown(self):
        self.proc.stop();self.clock.stop();self.store.close();self.tmp.cleanup()
    def put(self,p=None):
        p=p or position();self.b.state['position']=p;self.b.save()
        self.api.pos=[exchange_position(p)];self.api.stops=[protective_order(p)];return p
    def test_pending_is_durable_before_write(self):
        def check(path,body):
            saved=self.store.get('engine')['pending'];self.assertEqual(saved['cid'],body['clientOid'])
        self.api.before_post=check;self.b.entry(sig(),NOW)
        self.assertEqual(len(self.api.sent),1)
    def test_unknown_entry_is_not_resent_after_restart(self):
        self.api.fail=UnknownOrder('network uncertain');self.b.entry(sig(),NOW)
        self.assertIsNotNone(self.store.get('engine')['pending'])
        restarted=Live(self.api,self.store,self.c,lambda:True,self.root/'old');restarted.boot()
        for k in range(4):restarted.reconcile_pending(NOW+k*10_000)
        self.assertFalse(restarted.entry(sig(key='other'),NOW));self.assertEqual(len(self.api.sent),1)
    def test_explicit_rejection_clears_but_does_not_repeat_signal(self):
        self.api.fail=Rejected('insufficient funds');self.b.entry(sig(),NOW)
        self.assertIsNone(self.b.state['pending']);self.assertFalse(self.b.entry(sig(),NOW));self.assertEqual(len(self.api.sent),1)
    def test_ioc_partial_entry_accepts_actual_quantity_only(self):
        self.b.entry(sig(),NOW);pending=self.b.state['pending']
        qty=rounded(pending['sized']['qty']/2,.001);self.api.filled(pending,qty,3000,'cancelled')
        self.b.reconcile_pending(NOW+1000);p=self.b.state['position']
        self.assertEqual(p['qty'],qty);self.assertEqual(p['original_qty'],qty);self.assertIsNone(self.b.state['pending'])
        self.assertGreater(p['initial_risk_usdt'],0);self.assertLess(p['initial_risk_usdt'],p['risk_budget']*.6)
    def test_fills_from_another_order_are_not_adopted(self):
        self.b.entry(sig(),NOW);pending=self.b.state['pending'];self.api.filled(pending,pending['sized']['qty'],3000)
        self.api.fill_data[pending['oid']][0]['orderId']='foreign'
        with self.assertRaisesRegex(SafetyError,'OWNERSHIP'):self.b.reconcile_pending(NOW)
        self.assertIsNone(self.b.state['position']);self.assertIsNotNone(self.b.state['pending'])
    def test_post_fill_risk_exit_survives_restart(self):
        self.b.entry(sig(),NOW);pending=self.b.state['pending']
        self.api.filled(pending,pending['sized']['qty'],3150)
        self.b.reconcile_pending(NOW+1000)
        saved=self.store.get('engine');self.assertEqual(saved['halt'],'POST_FILL_RISK_EXCEEDED')
        self.assertEqual(saved['position']['force_exit'],'SAFETY_RISK_EXIT')
        restarted=Live(self.api,self.store,self.c,lambda:True,self.root/'old');restarted.boot()
        p=restarted.state['position'];self.api.pos=[exchange_position(p)];self.api.stops=[protective_order(p)]
        restarted.poll(NOW+20_000)
        self.assertEqual(restarted.state['pending']['reason'],'SAFETY_RISK_EXIT')
    def test_missing_stop_attempts_owned_reduce_only_close(self):
        p=self.put();self.api.stops=[];self.b.poll(NOW)
        self.assertEqual(self.b.state['halt'],'EXCHANGE_STOP_MISSING')
        pending=self.b.state['pending'];self.assertEqual(pending['kind'],'EXIT')
        self.assertEqual(pending['body']['reduceOnly'],'yes');self.assertEqual(pending['reason'],'SAFETY_NO_PROTECTION')
    def test_small_or_wrong_side_stop_does_not_pass(self):
        p=position();r=protective_order(p);r['qty']='0.001';self.assertFalse(account.protection([r],p))
        r=protective_order(p);r['posSide']='short';self.assertFalse(account.protection([r],p))
        self.assertEqual(account.protection([protective_order(p)],p),['stop-1'])
    def test_full_position_stop_supported(self):
        p=position();r=protective_order(p);r['qty']='0';r['tpslMode']='full'
        self.assertTrue(account.protection([r],p))
    def test_split_stops_cover_position_without_counting_duplicates(self):
        p=position();a=protective_order(p);b=protective_order(p)
        a['qty']=str(p['qty']/2);b['qty']=str(p['qty']/2);b['orderId']='stop-2'
        self.assertTrue(account.protection([a,b],p))
        self.assertFalse(account.protection([a,a],p))
    def test_external_quantity_change_is_not_closed(self):
        p=self.put();self.api.pos[0]['total']=str(p['qty']*2)
        self.b.close_position('STOP',NOW);self.assertEqual(len(self.api.sent),0);self.assertEqual(self.b.state['halt'],'POSITION_SIZE_CHANGED')
    def test_position_already_closed_never_creates_reverse_order(self):
        self.put();self.api.pos=[];self.b.close_position('STOP',NOW);self.assertEqual(len(self.api.sent),0)
    def test_hedge_close_uses_original_side(self):
        self.put(position('SHORT','hedge_mode'));self.b.close_position('STOP',NOW)
        body=self.api.sent[-1][1];self.assertEqual(body['side'],'buy');self.assertEqual(body['posSide'],'short')
    def test_unknown_exit_is_not_repeated(self):
        self.put();self.api.fail=UnknownOrder('uncertain');self.b.close_position('STOP',NOW)
        self.b.close_position('STOP',NOW+10_000);self.b.reconcile_pending(NOW+20_000)
        self.assertEqual(len(self.api.sent),1);self.assertEqual(self.b.state['pending']['kind'],'EXIT')
    def test_partial_exit_reduces_only_verified_remainder(self):
        p=self.put();total=p['qty'];self.b.close_position('STOP',NOW)
        pending=self.b.state['pending'];half=rounded(total/2,.001)
        self.api.filled(pending,half,2999,'cancelled');self.b.reconcile_pending(NOW+1000)
        self.assertAlmostEqual(p['qty'],total-half);self.assertEqual(p['original_qty'],total)
        self.api.pos=[exchange_position(p)];self.b.close_position('STOP',NOW+5000)
        self.assertAlmostEqual(float(self.api.sent[-1][1]['qty']),total-half)
        self.assertEqual(self.api.sent[-1][1]['reduceOnly'],'yes')
    def test_partial_exit_keeps_exact_exchange_quantity_increment(self):
        p=position();p['qty']=.067;p['original_qty']=.067;self.put(p)
        self.b.close_position('STOP',NOW);pending=self.b.state['pending']
        self.api.filled(pending,.033,2999,'cancelled');self.b.reconcile_pending(NOW+1000)
        self.api.pos=[exchange_position(p)];self.b.close_position('STOP',NOW+5000)
        self.assertEqual(Decimal(self.api.sent[-1][1]['qty'])%Decimal('.001'),0)
    def test_history_net_includes_funding_and_fees(self):
        p=self.put();self.api.historical=[history(p,-2.)];self.api.pos=[]
        self.b.poll(NOW);t=self.store.trades()[0]
        self.assertAlmostEqual(t['net_usdt'],-2);self.assertAlmostEqual(t['funding_usdt'],-.03)
        self.assertIsNone(self.store.get('engine')['position']);self.assertEqual(self.b.state['consecutive_losses'],1)
        self.b.poll(NOW+20_000);self.assertEqual(len(self.store.trades()),1)
        self.assertEqual(self.b.state['consecutive_losses'],1)
    def test_other_history_is_not_assigned_to_owned_trade(self):
        p=self.put();h=history(p);h['createdTime']=str(p['opened']-DAY);self.api.historical=[h];self.api.pos=[]
        self.b.poll(NOW);self.assertIsNotNone(self.b.state['position']);self.assertEqual(self.store.trades(),[])
    def test_accounting_mismatch_halts(self):
        p=self.put();h=history(p);h['netProfit']='99';self.api.historical=[h];self.api.pos=[]
        self.b.poll(NOW);self.assertIn('ACCOUNTING_MISMATCH',self.b.state['halt']);self.assertEqual(self.store.trades(),[])
    def test_guard_blocks_entry_without_resetting_equity(self):
        risk.blocks(self.b.state,1060,self.c,NOW);self.b.save()
        with self.assertRaisesRegex(SafetyError,'DAILY'):self.b.entry(sig(),NOW)
        self.assertEqual(len(self.api.sent),0)
    def test_private_account_change_refused(self):
        self.store.set('account_uid','someone-else')
        with self.assertRaisesRegex(SafetyError,'ACCOUNT_CHANGED'):self.b.boot()
    def test_pause_preserves_exit_management(self):
        self.b.authorized=lambda:False;self.assertFalse(self.b.entry(sig(),NOW))
        p=self.put();self.b.tick(quote(price=p['stop']-1),None,NOW)
        self.assertEqual(self.b.state['pending']['kind'],'EXIT')
    def test_restart_keeps_original_management_parameters(self):
        p=self.put();p['config']['exit_channel']=8;self.b.save()
        restart=Live(self.api,self.store,self.c,lambda:True,self.root/'old');restart.boot()
        self.assertEqual(restart.c['exit_channel'],8)
    def test_paper_never_reports_unknown_funding_as_net_profit(self):
        paper=Paper(self.api,self.store,self.c);paper.state=risk.fresh();paper.state['cash']=1018.;paper.boot()
        paper.entry(sig(key='paper'),NOW);p=paper.state['position']
        paper.tick(quote(price=p['stop']-1),None,NOW+1000)
        t=self.store.trades()[0];self.assertIsNone(t['net_usdt']);self.assertIsNone(t['funding_usdt'])

class TransportTests(unittest.TestCase):
    def test_public_mode_cannot_write_or_read_private(self):
        api=Rest(opener=lambda *a,**k:self.fail('network must not be called'))
        with self.assertRaises(SafetyError):api.post('/api/v3/trade/place-order',{})
        with self.assertRaises(SafetyError):api.get('/api/v3/account/assets',private=True)
    def test_timeout_write_happens_once(self):
        calls=[]
        def opener(*a,**kw):calls.append(1);raise urllib.error.URLError('timeout')
        api=Rest({'key':'test','secret':'test','passphrase':'test'},write=True,opener=opener)
        with self.assertRaises(UnknownOrder):api.post('/api/v3/trade/place-order',{'clientOid':'once'})
        self.assertEqual(len(calls),1)
    def test_ambiguous_exchange_response_is_not_retried(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self):return b'{"code":"40010","data":null}'
        calls=[]
        def opener(*a,**kw):calls.append(1);return Response()
        api=Rest({'key':'test','secret':'test','passphrase':'test'},write=True,opener=opener)
        with self.assertRaises(UnknownOrder):api.post('/api/v3/trade/place-order',{})
        self.assertEqual(len(calls),1)
    def test_unknown_or_transfer_write_endpoint_refused(self):
        api=Rest({'key':'test','secret':'test','passphrase':'test'},write=True)
        with self.assertRaises(SafetyError):api.post('/api/v3/account/transfer',{})
    def test_missing_lists_are_not_assumed_empty(self):
        with self.assertRaises(DataError):rows({})
    def test_truncated_pagination_fails_closed(self):
        api=Rest();api.get=lambda *a,**k:{'list':[{}]*100}
        with self.assertRaisesRegex(DataError,'pagination'):api.pages('/x',{})
    def test_candle_pagination_keeps_boundary_bar(self):
        api=Rest();offset=16*3_600_000
        def get(path,params):
            if path.endswith('/candles'):indices=range(10,20)
            else:
                boundary=(int(params['endTime'])-offset)//DAY
                indices=range(boundary-10,boundary)
            return [[str(i*DAY+offset),'1','2','1','1','1','1'] for i in indices]
        api.get=get
        bars=api.candles('BTCUSDT','1D',20)
        self.assertEqual(len(bars),20)
        self.assertTrue(all(b.ts-a.ts==DAY for a,b in zip(bars,bars[1:])))
    def test_fills_deduplicate_and_preserve_unknown_fee(self):
        f={'execId':'a','execQty':'1','execPrice':'100','execPnl':'2','createdTime':'1','feeDetail':[{'feeCoin':'OTHER','fee':'.01'}]}
        s=fill_summary([f,f]);self.assertEqual(s['qty'],1);self.assertIsNone(s['fees'])
    def test_duplicate_process_lock(self):
        with tempfile.TemporaryDirectory() as t:
            with runtime.lock(t,'live'):
                with self.assertRaises(SafetyError):
                    with runtime.lock(t,'live'):pass
    def test_invalid_risk_config_refused(self):
        with tempfile.TemporaryDirectory() as t:
            c=conf();c['risk_pct']=2
            (Path(t)/'config.json').write_text(json.dumps(c))
            with self.assertRaises(SafetyError):config(t)
    def test_missing_config_key_refused(self):
        with tempfile.TemporaryDirectory() as t:
            c=conf();del c['max_spread_pct'];(Path(t)/'config.json').write_text(json.dumps(c))
            with self.assertRaises(SafetyError):config(t)

if __name__=='__main__':unittest.main()
