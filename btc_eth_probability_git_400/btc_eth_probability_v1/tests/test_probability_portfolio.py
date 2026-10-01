"""Durable dual-symbol orchestration; fake exchange only, no network calls."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from basic_core import account,risk
from basic_core.core import CAT,DataError,SafetyError,Instrument,Quote,now_ms
from basic_core.portfolio import Portfolio,LegStore
from basic_core.store import Store
from test_probability_execution import config,pending,position,exchange_position,stop,assets

class Exchange:
    def __init__(self):self.pos=[];self.stops=[];self.orders=[];self.writes=[];self.equity=910;self.margin=0
    def get(self,*a,**k):return {'userId':'test-account','permissions':['uta_trade','uta_mgt']}
    def instrument(self,s):return Instrument(s,.1,.0001,.0001,5,100,.0006)
    def settled_funding(self,symbol,now):return {'symbol':symbol,'timestamp':now,'rate':0.,'read_at':now}
    def positions(self,cat=CAT):return copy.deepcopy(self.pos) if cat==CAT else []
    def open_orders(self,cat=CAT):return copy.deepcopy(self.orders) if cat==CAT else []
    def strategies(self,cat=CAT,kind='tpsl'):return copy.deepcopy(self.stops) if cat==CAT and kind=='tpsl' else []
    def assets(self):
        a=assets(available=self.equity,effective=self.equity,initial=self.margin,usd_value=self.equity)
        a['usdtEquity']=str(self.equity)
        a['assets'][0].update(equity=str(self.equity),balance=str(self.equity))
        return a
    def settings(self):return {'accountMode':'unified','holdMode':'one_way_mode','symbolConfigList':[
        {'symbol':s,'category':CAT,'marginMode':'crossed','leverage':'5'} for s in ('BTCUSDT','ETHUSDT')]}
    def quote(self,s):return Quote(s,now_ms(),3000,2999.9,3000.1,3000,3000,0)
    def post(self,path,body):self.writes.append((path,copy.deepcopy(body)));return {'orderId':'new-order'}

class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'live.sqlite');self.api=Exchange();self.c=config()
        self.b=self.new_broker();self.b.boot()
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def new_broker(self):return Portfolio(self.api,self.store,self.c,lambda:True,
        self.root/'legacy',self.root/'trend',basic_root=self.root/'basic')
    def hold(self,symbol='BTCUSDT'):
        now=now_ms()-30_000;p=position(now);p.update(entry_fee=.9,protection_ids=['stop-'+symbol],
            bound_protection_ids=['stop-'+symbol],symbol=symbol)
        ep=exchange_position(now);ep['symbol']=symbol
        sl=stop('stop-'+symbol);sl['symbol']=symbol
        self.b.legs[symbol].state['position']=p;self.api.pos.append(ep);self.api.stops.append(sl)
        self.api.margin+=400;self.b.save();return p
    def test_confirmed_btc_permits_eth_and_real_global_collateral(self):
        self.hold();self.b.refresh(True)
        self.assertTrue(self.b.can_enter('ETHUSDT'));self.assertFalse(self.b.can_enter('BTCUSDT'))
        view=self.b.legs['ETHUSDT'].api.entry_snapshot()
        self.assertEqual(view['positions'],[]);self.assertEqual(view['equity'],400)
        self.assertEqual(account.available_usdt(view['assets']),510)
        self.assertEqual(self.b.state['equity'],800)
        self.assertEqual(self.b.state['legs']['BTCUSDT']['cash'],400)
        self.assertEqual(self.b.state['legs']['ETHUSDT']['cash'],400)
    def test_old_500_allocation_database_cannot_silently_change_risk_accounting(self):
        old=self.store.get('engine');old['allocation_usdt']=500;old['target_notional_usdt']=2500
        old['legs']['BTCUSDT']['cash']=500;self.store.set('engine',old)
        with self.assertRaisesRegex(SafetyError,'INCOMPATIBLE_SIZING_DATABASE'):
            self.new_broker()
        old.pop('allocation_usdt');old.pop('target_notional_usdt');self.store.set('engine',old)
        with self.assertRaisesRegex(SafetyError,'INCOMPATIBLE_SIZING_DATABASE'):
            self.new_broker()
    def test_both_confirmed_positions_valid_without_netting(self):
        self.hold();self.hold('ETHUSDT');self.b.refresh(True)
        self.assertEqual(len(self.b.snapshot['positions']),2);self.assertIsNone(self.b.state['halt'])
    def test_second_symbol_dispatch_uses_2000_and_its_own_side(self):
        self.hold();self.b.refresh(True)
        sized={'qty':.66,'limit':3000,'stop':2990,'fee_rate':.0006,'risk_budget':20,'notional':1980,'margin':396}
        sig={'symbol':'ETHUSDT','side':'LONG','key':'ETH-test','bar_end':now_ms()-60_000}
        with patch('basic_core.engine.strategy.size',return_value=sized),patch('basic_core.cli.legacy_process_guard'):
            self.assertTrue(self.b.entry(sig,now_ms()))
        self.assertEqual(len(self.api.writes),1);body=self.api.writes[0][1]
        self.assertEqual(body['symbol'],'ETHUSDT');self.assertEqual(body['qty'],'0.66')
        self.assertIsNotNone(self.b.legs['BTCUSDT'].state['position'])
    def test_second_symbol_insufficient_free_margin_does_not_downsize(self):
        self.hold();self.api.margin=420
        sized={'qty':.66,'limit':3000,'stop':2990,'fee_rate':.0006,'risk_budget':20,'notional':1980,'margin':396}
        sig={'symbol':'ETHUSDT','side':'LONG','key':'ETH-test','bar_end':now_ms()-60_000}
        with patch('basic_core.engine.strategy.size',return_value=sized),patch('basic_core.cli.legacy_process_guard'):
            with self.assertRaisesRegex(SafetyError,'INSUFFICIENT_AVAILABLE_MARGIN'):self.b.entry(sig,now_ms())
        self.assertEqual(self.api.writes,[])
    def test_pending_entry_survives_restart_blocks_other_symbol(self):
        self.b.legs['BTCUSDT'].state['pending']=pending(now_ms());self.b.save()
        restarted=self.new_broker();restarted.boot()
        self.assertFalse(restarted.can_enter('ETHUSDT'));self.assertEqual(self.api.writes,[])
        self.assertEqual(restarted.legs['BTCUSDT'].state['pending']['cid'],'PTE_test')
    def test_partial_stop_before_position_defers_without_permanent_halt(self):
        self.b.legs['BTCUSDT'].state['pending']=pending(now_ms());self.b.save()
        sl=stop();sl['qty']='.008';self.api.stops=[sl]
        with self.assertRaisesRegex(DataError,'PENDING_ENTRY_STRATEGY_PROPAGATION'):self.b.refresh(True)
        self.assertIsNone(self.b.state['halt']);self.assertEqual(self.b.last_refresh,0)
        self.assertFalse(self.b.can_enter('ETHUSDT'))
        restarted=self.new_broker();restarted.boot()
        self.assertIsNone(restarted.state['halt']);self.assertFalse(restarted.can_enter('ETHUSDT'))
    def test_partial_position_uses_actual_quantity_for_coverage(self):
        now=now_ms();self.b.legs['BTCUSDT'].state['pending']=pending(now)
        ep=exchange_position(now);ep['total']='.008';sl=stop();sl['qty']='.008'
        self.api.pos=[ep];self.api.stops=[sl];self.b.refresh(True)
        self.assertIsNone(self.b.state['halt']);self.assertFalse(self.b.can_enter('ETHUSDT'))
    def test_foreign_stop_not_hidden_by_partial_propagation(self):
        self.b.legs['BTCUSDT'].state['pending']=pending(now_ms())
        sl=stop();sl['qty']='.008';other=stop('foreign');other['symbol']='SOLUSDT'
        self.api.stops=[sl,other]
        with self.assertRaisesRegex(SafetyError,'UNMANAGED_STRATEGY_ORDER'):self.b.refresh(True)
        self.assertEqual(self.b.state['halt'],'UNMANAGED_STRATEGY_ORDER')
    def test_missing_fresh_stop_invalidates_cached_protection_before_eth_entry(self):
        p=self.hold();self.b.refresh(True);self.assertTrue(self.b.can_enter('ETHUSDT'))
        self.api.stops=[];self.b.refresh(True)
        self.assertEqual(p['protection_ids'],[]);self.assertEqual(p['force_exit'],'SAFETY_NO_PROTECTION')
        self.assertFalse(self.b.can_enter('ETHUSDT'));self.assertEqual(self.b.state['halt'],'EXCHANGE_STOP_MISSING')
    def test_initial_stop_propagation_grace_blocks_entry_without_halt(self):
        p=self.hold();p['protect_deadline']=now_ms()+15000;self.api.stops=[];self.b.refresh(True)
        self.assertIsNone(self.b.state['halt']);self.assertFalse(self.b.can_enter('ETHUSDT'))
    def test_closing_position_missing_stop_does_not_false_halt(self):
        p=self.hold();p['closing']=True;self.api.stops=[];self.b.refresh(True)
        self.assertIsNone(self.b.state['halt']);self.assertFalse(self.b.can_enter('ETHUSDT'))
    def test_latched_exit_prevents_other_entry(self):
        p=self.hold();self.b.refresh(True);p['force_exit']='OBSERVED_STRUCTURAL_STOP'
        self.assertFalse(self.b.can_enter('ETHUSDT'))
    def test_600_account_rejected_without_latching_halt(self):
        self.b.state['baseline_account_equity']=None;self.api.equity=600
        with self.assertRaisesRegex(SafetyError,'INSUFFICIENT_910'):self.b.refresh(True)
        self.assertIsNone(self.b.state['halt']);self.assertIsNone(self.b.state['baseline_account_equity'])
    def test_daily_and_weekly_blocks_preserve_existing_structural_exit(self):
        p=self.hold();self.api.equity=810;self.b.refresh(True)
        self.assertIn('DAILY_LOSS_LIMIT',self.b.state['entry_blocks'])
        self.assertIn('WEEKLY_LOSS_LIMIT',self.b.state['entry_blocks'])
        self.assertEqual(p.get('force_exit'),'ACCOUNT_PERIOD_LOSS_LIMIT');self.assertFalse(self.b.can_enter('ETHUSDT'))
    def test_max_drawdown_is_explicit_emergency_exit(self):
        p=self.hold();self.api.equity=700;self.b.refresh(True)
        self.assertEqual(p['force_exit'],'ACCOUNT_DRAWDOWN_EMERGENCY')
        self.assertEqual(self.b.state['halt'],'MAX_DRAWDOWN')
    def test_stale_price_freezes_equity_without_inventing_35_loss(self):
        self.hold();self.b.legs['BTCUSDT'].state['equity']=397.2
        self.assertEqual(self.b.leg_equity('BTCUSDT'),397.2)
        self.b.quotes['BTCUSDT']=Quote('BTCUSDT',now_ms()-60000,90000,90000,90001,90000,90000,0)
        self.assertEqual(self.b.leg_equity('BTCUSDT'),397.2)
    def test_one_leg_poll_error_does_not_skip_other_leg(self):
        with patch.object(self.b.legs['BTCUSDT'],'poll',side_effect=DataError('test')),patch.object(self.b.legs['ETHUSDT'],'poll') as second:
            with self.assertRaises(DataError):self.b.poll(now_ms())
            second.assert_called_once()
    def test_flat_leg_metadata_outage_does_not_abort_other_owned_leg(self):
        self.hold('ETHUSDT');restarted=self.new_broker();original=self.api.instrument
        def instrument(symbol):
            if symbol=='BTCUSDT':raise DataError('metadata unavailable')
            return original(symbol)
        with patch.object(self.api,'instrument',side_effect=instrument):restarted.boot()
        self.assertIsNotNone(restarted.legs['ETHUSDT'].state['position']);self.assertFalse(restarted.can_enter('BTCUSDT'))
    def test_owned_recovery_uses_frozen_steps_when_instrument_read_fails(self):
        self.hold();restarted=self.new_broker();original=self.api.instrument
        def instrument(symbol):
            if symbol=='BTCUSDT':raise DataError('metadata unavailable')
            return original(symbol)
        with patch.object(self.api,'instrument',side_effect=instrument):restarted.boot()
        self.assertEqual(restarted.legs['BTCUSDT'].instruments['BTCUSDT'].qty_step,.0001)
        self.assertFalse(restarted.can_enter('ETHUSDT'));self.assertIn('BTCUSDT',restarted.metadata_errors)
        restarted.refresh(True);self.assertEqual(restarted.metadata_errors,{})
    def test_foreign_order_on_restart_blocks_entry_but_owned_manager_boots(self):
        self.hold();self.api.orders=[{'symbol':'SOLUSDT','category':CAT,'clientOid':'foreign'}]
        restarted=self.new_broker();restarted.boot()
        self.assertEqual(restarted.state['halt'],'UNMANAGED_OPEN_ORDER')
        self.assertIsNotNone(restarted.legs['BTCUSDT'].state['position'])
        self.assertFalse(restarted.can_enter('ETHUSDT'))
        with patch.object(restarted.legs['BTCUSDT'],'tick') as tick:
            restarted.tick(Quote('BTCUSDT',now_ms(),100000,99999,100001,100000,100000,0),None,now_ms())
            tick.assert_called_once()
    def test_old_basic_arm_blocks_entries(self):
        p=self.root/'basic/data/LIVE_ENABLED.json';p.parent.mkdir(parents=True);p.write_text('{}')
        self.assertFalse(self.b.can_enter('ETHUSDT'))
    def test_old_basic_open_leg_blocks_migration(self):
        old=Store(self.root/'basic/data/live.sqlite');old.set('engine',{'legs':{'BTCUSDT':{'position':{'id':'old'}}}});old.close()
        self.b.state['migrated']=False;self.b.save()
        with self.assertRaisesRegex(SafetyError,'LEGACY_POSITION_OR_PENDING'):self.new_broker().boot()
    def test_final_accounting_is_durable_and_not_double_counted(self):
        leg=self.b.legs['BTCUSDT'];trade={'id':'done','closed':now_ms(),'net_usdt':-10,'config':self.c}
        risk.closed(leg.state,-10,self.c,trade['closed']);LegStore(self.b,'BTCUSDT').finish(trade,leg.state)
        saved=self.store.get('engine');self.assertEqual(saved['legs']['BTCUSDT']['cash'],390)
        self.assertGreater(saved['legs']['BTCUSDT']['cooldown_until'],trade['closed'])
        self.assertEqual(saved['portfolio_guard']['cooldown_until'],trade['closed'])
        with self.assertRaisesRegex(SafetyError,'DUPLICATE_TRADE_FINALIZATION'):LegStore(self.b,'BTCUSDT').finish(trade,leg.state)
        self.assertEqual(len(self.store.trades()),1);self.assertEqual(self.b.legs['BTCUSDT'].state['cash'],390)

if __name__=='__main__':unittest.main()
