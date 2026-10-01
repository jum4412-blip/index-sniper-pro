"""Execution failure and ownership tests. No network or credentials are used."""
import copy,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from basic_core import account,risk
from basic_core.api import Rest
from basic_core.core import CAT,DataError,UnknownOrder,Instrument,Quote,now_ms
from basic_core.engine import Live

class MemoryStore:
    def __init__(self):self.data={};self.events=[]
    def get(self,k,default=None):return copy.deepcopy(self.data.get(k,default))
    def set(self,k,v):self.data[k]=copy.deepcopy(v)
    def event(self,k,v):self.events.append((k,copy.deepcopy(v)))
    def seen(self,k):return False
    def signal(self,k,v):pass

class FakeAPI:
    def __init__(self):self.writes=[];self.info=None;self.executions=[];self.position=[];self.stops=[];self.unknown=False
    def post(self,path,body):
        self.writes.append((path,copy.deepcopy(body)))
        if self.unknown:raise UnknownOrder('ambiguous')
        return {'orderId':'oid1'}
    def order(self,oid='',cid=''):
        if self.info is None:raise DataError('await propagation')
        return self.info
    def fills(self,oid):return self.executions
    def positions(self,*a):return self.position
    def strategies(self,*a):return self.stops
    def assets(self):return {'usdtEquity':'500'}
    def instrument(self,symbol):return Instrument(symbol,.1,.0001,.0001,5,10,.0006)
    def settled_funding(self,symbol,now):return {'symbol':symbol,'timestamp':now,'rate':0.,'read_at':now}

def config():return json.loads((ROOT/'config.json').read_text())
def setup():
    api=FakeAPI();store=MemoryStore();c=config();live=Live(api,store,c,lambda:False,ROOT)
    live.instruments['BTCUSDT']=Instrument('BTCUSDT',.1,.0001,.0001,5,10,.0006)
    return live,api,store

def pending(now,qty=.025,price=100_000,stop=99_600):
    c=config();return {'kind':'ENTRY','cid':'PTE_test','oid':'oid1','created':now,
        'body':{'symbol':'BTCUSDT','qty':str(qty),'side':'buy'},
        'sized':{'qty':qty,'stop':stop,'fee_rate':.0006,'risk_budget':25},
        'signal':{'symbol':'BTCUSDT','side':'LONG','bar_end':now-60_000},
        'equity':500,'hold_mode':'one_way_mode','config':c}

def fill(api,now,qty,price,cid='PTE_test'):
    api.info={'symbol':'BTCUSDT','orderId':'oid1','clientOid':cid,'orderStatus':'filled','cumExecQty':str(qty)}
    api.executions=[{'symbol':'BTCUSDT','orderId':'oid1','execId':'exec1','execQty':str(qty),'execPrice':str(price),
                     'execPnl':'0','feeDetail':[{'feeCoin':'USDT','fee':'-0.5'}],'createdTime':str(now)}]

def position(now,hold='one_way_mode'):
    return {'id':'PTE_test','symbol':'BTCUSDT','side':'LONG','opened':now,'entry':100_000,'qty':.025,
        'original_qty':.025,'initial_stop':99_600,'stop':99_600,'price_step':.1,'qty_step':.0001,
        'hold_mode':hold,'protect_deadline':now-1,'config':config(),'rejected_exits':0,
        'risk_budget':25,'last_quote_at':now,'fee_rate':.0006}
def exchange_position(now):return {'symbol':'BTCUSDT','category':CAT,'posSide':'long','total':'.025','createdTime':str(now),'marginMode':'crossed','leverage':'5'}
def stop(oid='stop1'):return {'symbol':'BTCUSDT','category':CAT,'posSide':'long','status':'pending','slTriggerBy':'mark','slOrderType':'market','stopLoss':'99600','qty':'.025','orderId':oid}
def assets(available=1110,effective=1110,initial=0,usd_value=1110):
    return {'usdtEquity':'1110','effEquity':str(effective),'imr':str(initial),'assets':[
        {'coin':'USDT','equity':'1110','usdValue':str(usd_value),'balance':'1110','available':str(available),'debt':'0','locked':'0'}]}

class ExecutionTests(unittest.TestCase):
    def enter(self,free=1110,hold='one_way_mode',side='LONG'):
        live,api,store=setup();now=now_ms();live.authorized=lambda:True
        api.entry_snapshot=lambda:{'positions':[],'orders':[],'strategies':[],'equity':400,
            'assets':assets(available=free),'settings':{'accountMode':'unified','holdMode':hold,
            'symbolConfigList':[{'symbol':'BTCUSDT','category':CAT,'marginMode':'crossed','leverage':'5'}]}}
        api.quote=lambda symbol:Quote(symbol,now,100_000,99_999,100_001,100_000,100_000,0)
        sig={'symbol':'BTCUSDT','side':side,'bar_end':now-60_000,'key':'signal1'}
        sized={'qty':.020,'limit':100_000 if side=='LONG' else 99_950,'stop':99_600 if side=='LONG' else 100_400,'fee_rate':.0006,'risk_budget':20,'notional':2000,'margin':400}
        with patch('basic_core.engine.risk.blocks',return_value=[]),patch('basic_core.engine.risk.legacy_check'),patch('basic_core.cli.legacy_process_guard'),patch('basic_core.engine.strategy.size',return_value=sized):
            live.entry(sig,now)
        return live,api,store
    def test_entry_attaches_initial_stop_and_uses_five_x_once(self):
        live,api,store=self.enter();body=api.writes[0][1]
        self.assertEqual(body['timeInForce'],'ioc');self.assertEqual(body['stopLoss'],'99600')
        self.assertEqual(body['slTriggerBy'],'mark');self.assertEqual(body['slOrderType'],'market')
        self.assertEqual(body['qty'],'0.02');self.assertEqual(body['reduceOnly'],'no')
        self.assertAlmostEqual(live.state['pending']['sized']['required_free_margin_usdt'],502.4)
    def test_entry_rejects_missing_fee_and_margin_reserve(self):
        with self.assertRaisesRegex(Exception,'INSUFFICIENT_AVAILABLE_MARGIN'):self.enter(free=502.3)
    def test_short_margin_uses_sizing_quote_not_lower_ioc_floor(self):
        with self.assertRaisesRegex(Exception,'INSUFFICIENT_AVAILABLE_MARGIN'):self.enter(free=502.3,side='SHORT')
        live,api,store=self.enter(free=502.4,side='SHORT')
        self.assertAlmostEqual(live.state['pending']['sized']['required_free_margin_usdt'],502.4)
    def test_hedge_entry_omits_oneway_reduce_only(self):
        live,api,store=self.enter(hold='hedge_mode');body=api.writes[0][1]
        self.assertEqual(body['posSide'],'long');self.assertNotIn('reduceOnly',body)
    def test_available_margin_subtracts_existing_position_initial_margin(self):
        self.assertAlmostEqual(account.available_usdt(assets(initial=500)),610)
    def test_available_margin_respects_coin_free_balance_and_conversion(self):
        self.assertAlmostEqual(account.available_usdt(assets(available=100)),100)
        self.assertAlmostEqual(account.available_usdt(assets(effective=500,initial=250,usd_value=500)),555.0)
    def test_margin_has_no_equity_only_fallback(self):
        with self.assertRaises(DataError):account.available_usdt({'usdtEquity':'10000'})
    def test_margin_missing_field_and_duplicate_coin_fail(self):
        a=assets();del a['assets'][0]['available']
        with self.assertRaises(DataError):account.available_usdt(a)
        a=assets();a['assets'].append(copy.deepcopy(a['assets'][0]))
        with self.assertRaises(DataError):account.available_usdt(a)
    def test_margin_rejects_borrowing_and_other_collateral(self):
        a=assets();a['assets'][0]['debt']='1'
        with self.assertRaisesRegex(Exception,'BORROWING'):account.available_usdt(a)
        a=assets();a['assets'].append({**a['assets'][0],'coin':'ETH','equity':'1','usdValue':'2000'})
        with self.assertRaisesRegex(Exception,'NON_USDT'):account.available_usdt(a)
    def test_insufficient_effective_margin_returns_zero(self):
        self.assertEqual(account.available_usdt(assets(initial=1200)),0)
    def test_unknown_entry_recovers_without_resending(self):
        live,api,store=setup();now=now_ms();p=pending(now);live.state['pending']=p;live.save();api.unknown=True
        live._send(p);self.assertEqual(len(api.writes),1)
        restarted=Live(api,store,config(),lambda:False,ROOT)
        restarted.reconcile_pending(now+1000);restarted.reconcile_pending(now+2000)
        self.assertEqual(len(api.writes),1);self.assertEqual(restarted.state['pending']['cid'],'PTE_test')
    def test_null_order_id_is_reconciled_by_client_id(self):
        live,api,store=setup();p=pending(now_ms());live.state['pending']=p;live.save()
        with patch.object(api,'post',return_value={'orderId':None}):live._send(p)
        self.assertIsNotNone(live.state['pending'])
    def test_partial_entry_uses_actual_filled_quantity(self):
        live,api,store=setup();now=now_ms();live.state['pending']=pending(now);fill(api,now,.008,100_000)
        api.info['orderStatus']='cancelled';live.reconcile_pending(now)
        p=live.state['position'];self.assertEqual(p['qty'],.008);self.assertIsNone(live.state['pending'])
        self.assertEqual(p['exchange_stop'],99_600);self.assertEqual(p['protection_mode'],'EXCHANGE_INITIAL_STOP_LOCAL_2R_TIME_EXIT')
        self.assertFalse(p['force_exit']);self.assertEqual(api.writes,[])
    def test_postfill_notional_violation_requests_exit(self):
        live,api,store=setup();now=now_ms();live.state['pending']=pending(now,stop=100_000);fill(api,now,.025,102_000)
        live.reconcile_pending(now)
        self.assertEqual(live.state['position']['force_exit'],'SAFETY_NOTIONAL_EXIT')
    def test_favourable_short_price_tolerance_is_explicit(self):
        live,api,store=setup();now=now_ms();p=pending(now,qty=.020,stop=100_400);p['signal']['side']='SHORT';p['body']['side']='sell'
        live.state['pending']=p;fill(api,now,.020,100_020);live.reconcile_pending(now)
        self.assertIsNone(live.state['position']['force_exit']);self.assertGreater(live.state['position']['entry_notional_usdt'],1500)
    def test_risk_uses_frozen_entry_config(self):
        live,api,store=setup();now=now_ms();live.state['pending']=pending(now,qty=.020,stop=97_000);fill(api,now,.020,100_000)
        live.c['max_trade_risk_usdt']=1000;live.reconcile_pending(now)
        self.assertEqual(live.state['position']['force_exit'],'SAFETY_RISK_EXIT')
    def test_net_exit_is_reduce_only_even_while_entries_disabled(self):
        live,api,store=setup();now=now_ms();live.state['position']=position(now);api.position=[exchange_position(now)]
        live.close_position('STRUCTURE',now);b=api.writes[0][1]
        self.assertEqual(b['reduceOnly'],'yes');self.assertNotIn('posSide',b);self.assertEqual(b['side'],'sell')
    def test_observed_breach_exits_after_rebound_without_waiting_for_poll(self):
        live,api,store=setup();now=now_ms();p=position(now);p['force_exit']='OBSERVED_STRUCTURAL_STOP'
        live.state['position']=p;api.position=[exchange_position(now)]
        q=Quote('BTCUSDT',now,100_000,99_999,100_001,100_000,100_000,0)
        with patch('basic_core.engine.strategy.manage') as manage:live.tick(q,None,now)
        manage.assert_not_called();self.assertEqual(len(api.writes),1)
        self.assertEqual(live.state['pending']['reason'],'OBSERVED_STRUCTURAL_STOP')
    def test_hedge_exit_identifies_position_side(self):
        live,api,store=setup();now=now_ms();live.state['position']=position(now,'hedge_mode');api.position=[exchange_position(now)]
        live.close_position('STRUCTURE',now);b=api.writes[0][1]
        self.assertEqual(b['posSide'],'long');self.assertNotIn('reduceOnly',b)
    def test_partial_exit_tracks_remainder_no_replay(self):
        live,api,store=setup();now=now_ms();live.state['position']=position(now);api.position=[exchange_position(now)]
        live.close_position('STRUCTURE',now);cid=live.state['pending']['cid'];fill(api,now,.005,99_000,cid)
        live.reconcile_pending(now+10);self.assertAlmostEqual(live.state['position']['qty'],.020)
        self.assertIsNone(live.state['pending']);self.assertEqual(len(api.writes),1)
    def test_bound_stop_cannot_be_replaced_by_foreign_same_price_order(self):
        p=position(now_ms());p['bound_protection_ids']=['stop1']
        self.assertEqual(account.protection([stop()],p),['stop1'])
        self.assertEqual(account.protection([stop('manual')],p),[])
    def test_owned_stop_remains_initial_while_local_stop_trails(self):
        p=position(now_ms());p['stop']=100_500
        self.assertEqual(account.protection([stop()],p),['stop1'])
    def test_stale_trail_diagnostic_does_not_cancel_exchange_stop(self):
        live,api,store=setup();now=now_ms();p=position(now-60_000);live.state['position']=p
        api.position=[exchange_position(p['opened'])];api.stops=[stop()]
        with patch('basic_core.engine.risk.blocks',return_value=[]):live.poll(now)
        self.assertTrue(p['local_trail_stale']);self.assertEqual(p['bound_protection_ids'],['stop1'])
        self.assertIn('LOCAL_TRAIL_STALE',[k for k,v in store.events]);self.assertEqual(api.writes,[])
    def test_daily_guard_closes_owned_position(self):
        live,api,store=setup();now=now_ms();p=position(now);live.state['position']=p;api.position=[exchange_position(now)];api.stops=[stop()]
        with patch('basic_core.engine.risk.blocks',return_value=['DAILY_LOSS_LIMIT']):live.poll(now)
        self.assertEqual(p.get('force_exit'),'ACCOUNT_LOSS_LIMIT');self.assertEqual(len(api.writes),1)
    def test_fresh_local_trail_saved_without_tpsl_write(self):
        live,api,store=setup();now=now_ms();live.state['position']=position(now)
        q=Quote('BTCUSDT',now,100_000,99_999,100_001,100_000,100_000,0)
        def manage(p,*a):p['stop']=99_800
        with patch('basic_core.engine.strategy.manage',side_effect=manage):live.tick(q,None,now)
        self.assertEqual(store.get('engine')['position']['stop'],99_800);self.assertEqual(api.writes,[])
    def test_loosened_trail_restored_and_halted(self):
        live,api,store=setup();now=now_ms();live.state['position']=position(now)
        q=Quote('BTCUSDT',now,100_000,99_999,100_001,100_000,100_000,0)
        def manage(p,*a):p['stop']=97_000
        with patch('basic_core.engine.strategy.manage',side_effect=manage):live.tick(q,None,now)
        self.assertEqual(live.state['position']['stop'],99_600);self.assertEqual(live.state['halt'],'LOCAL_TRAIL_LOOSENED')
    def test_one_ticker_request_contains_both_symbols(self):
        api=Rest();now=now_ms()
        data=[{'symbol':s,'ts':str(now),'lastPrice':'100','bid1Price':'99','ask1Price':'101','markPrice':'100','indexPrice':'100','fundingRate':'0'} for s in ('BTCUSDT','ETHUSDT')]
        with patch.object(api,'get',return_value=data) as get:
            self.assertEqual(set(api.quotes()),{'BTCUSDT','ETHUSDT'});self.assertEqual(get.call_count,1)
    def test_missing_ticker_timestamp_fails_closed(self):
        d={'symbol':'BTCUSDT','lastPrice':'100','bid1Price':'99','ask1Price':'101','markPrice':'100','indexPrice':'100','fundingRate':'0'}
        with self.assertRaises((DataError,KeyError)):Rest._quote(d,'BTCUSDT')
    def test_candle_first_page_supports_1000(self):
        api=Rest();rows=[[i*60_000,100,101,99,100,1,100] for i in range(300)]
        with patch.object(api,'get',return_value=rows) as get:
            result=api.candles('BTCUSDT','1m',300)
            self.assertEqual(len(result),300);self.assertEqual(get.call_args.args[1]['limit'],'300')
    def test_missing_history_fails_closed(self):
        with patch.object(Rest,'get',return_value=[]):
            with self.assertRaises(DataError):Rest().candles('BTCUSDT','1m',300)

if __name__=='__main__':unittest.main()
