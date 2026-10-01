"""Dust is not margin; recovery must preserve every unrelated risk record."""
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from basic_core import account, recovery
from basic_core.core import SafetyError, DataError
from basic_core.portfolio import fresh
from basic_core.store import Store


def asset(coin='USDT',quantity=1000,value=1000):
    return {'coin':coin,'equity':str(quantity),'balance':str(quantity),'available':str(quantity),
            'usdValue':str(value),'debt':'0','locked':'0'}


def sample():
    coins=[asset('USDT',1000,999.5),
           asset('HYPE',.008,.8),asset('TAO',.0005,.16)]
    # Synthetic fixtures reproduce the response shape, without real balances.
    return {'effEquity':str(sum(float(c['usdValue']) for c in coins)),
            'imr':'0','usdtEquity':'1000.96','assets':coins}


class DustTests(unittest.TestCase):
    def test_running_or_ambiguous_service_blocks_halt_recovery(self):
        for output in ('ActiveState=active\nMainPID=123\n',
                       'ActiveState=activating\nMainPID=0\n',
                       'ActiveState=inactive\nMainPID=123\n',
                       'ActiveState=inactive\n'):
            result=subprocess.CompletedProcess([],0,stdout=output,stderr='')
            with patch('basic_core.recovery.Path.exists',return_value=True), \
                 patch('basic_core.recovery.subprocess.run',return_value=result), \
                 self.assertRaisesRegex(SafetyError,'STOP_NEW_SERVICE'):
                recovery.service_stopped(ROOT)

    def test_reported_shape_passes_and_dust_is_excluded(self):
        result=account.margin_breakdown(sample())
        self.assertAlmostEqual(result['excluded_dust_usd'],.96)
        self.assertEqual(result['dust_coins'],['HYPE','TAO'])
        self.assertAlmostEqual(result['available_usdt'],1000)

    def test_effective_margin_excludes_full_value_even_without_collateral_credit(self):
        value=sample();value['effEquity']=value['assets'][0]['usdValue']
        self.assertLess(account.available_usdt(value),float(value['assets'][0]['available']))
        value['imr']='500'
        expected=(float(value['effEquity'])-500-.96)*1000/999.5
        self.assertAlmostEqual(account.available_usdt(value),expected)

    def test_available_coin_balance_remains_a_hard_cap(self):
        value=sample();value['assets'][0]['available']='200'
        self.assertEqual(account.available_usdt(value),200)

    def test_limit_is_aggregate_not_per_coin(self):
        value=sample()
        value['assets'][1]['usdValue']='.6';value['assets'][2]['usdValue']='.6'
        with self.assertRaisesRegex(SafetyError,'NON_USDT'):account.available_usdt(value)

    def test_one_dollar_boundary_and_zero_balances(self):
        value=sample();value['assets']=[asset(),asset('HYPE',.1,1),asset('TAO',0,0)]
        self.assertEqual(account.margin_breakdown(value)['excluded_dust_usd'],1)
        value['assets'][1]['usdValue']='1.00000001'
        with self.assertRaisesRegex(SafetyError,'NON_USDT'):account.available_usdt(value)

    def test_debt_locked_negative_unpriced_and_nonfinite_dust_fail(self):
        for field,bad in (('debt','0.00001'),('locked','.00001'),('balance','-.1'),
                          ('equity','0'),('usdValue','0'),('usdValue','nan'),('usdValue','inf')):
            value=sample();value['assets'][1][field]=bad
            with self.subTest(field=field,bad=bad),self.assertRaises(SafetyError):
                account.available_usdt(value)

    def test_missing_and_duplicate_dust_fields_fail(self):
        value=sample();del value['assets'][1]['usdValue']
        with self.assertRaises(DataError):account.available_usdt(value)
        value=sample();value['assets'].append(copy.deepcopy(value['assets'][1]))
        with self.assertRaises(DataError):account.available_usdt(value)

    def test_dust_does_not_make_624_5_usdt_sufficient_for_625(self):
        value={'effEquity':'625.25','imr':'0','assets':[asset(quantity=624.5,value=624.5),asset('HYPE',.01,.75)]}
        self.assertEqual(account.available_usdt(value),624.5)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.c=json.loads((ROOT/'config.json').read_text())
        (self.root/'config.json').write_text(json.dumps(self.c))
        self.path=self.root/'data/live.sqlite'
        state=fresh(self.c)
        state['halt']=recovery.COLLATERAL_HALT
        state['migrated']=True
        state['account_guard'].update(peak_equity=1500,day_start_equity=1200,
                                      day_blocked=True,week_blocked=True,
                                      consecutive_losses=3,loss_pause_until=9999999999999)
        state['legs']['BTCUSDT']['halt']='MAX_DRAWDOWN'
        self.before=copy.deepcopy(state)
        store=Store(self.path);store.set('engine',state);store.set('account_uid','owned-account');store.close()
        self.uid='owned-account';self.positions=[];self.orders=[];self.assets=sample();self.assets['assets'][0]=asset('USDT',1200,1199.5);self.assets['effEquity']='1200.46'
        parent=self
        class ReadOnly:
            def get(self,*args,**kwargs):return {'userId':parent.uid}
            def post(self,*args,**kwargs):raise AssertionError('unexpected exchange write')
        self.api=ReadOnly()
        self.stack=self.enterContext(__import__('contextlib').ExitStack())
        self.stack.enter_context(patch('basic_core.recovery.service_stopped'))
        self.stack.enter_context(patch('basic_core.cli.legacy_state_guard'))
        self.stack.enter_context(patch('basic_core.cli.legacy_process_guard'))
        self.stack.enter_context(patch('basic_core.recovery.time.sleep'))
        self.stack.enter_context(patch('basic_core.account.inventory',side_effect=self.snapshot))

    def snapshot(self,*args):
        return {'positions':self.positions,'orders':self.orders,'strategies':[],
                'equity':1200.96,'assets':copy.deepcopy(self.assets)}

    def state(self):
        with sqlite3.connect(self.path) as db:
            return json.loads(db.execute("SELECT json FROM state WHERE key='engine'").fetchone()[0])

    def write_state(self,state):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE state SET json=? WHERE key='engine'",(json.dumps(state),))

    def test_recovery_backs_up_and_only_changes_specific_top_level_halt(self):
        result=recovery.recover_collateral(self.root,self.api)
        self.assertEqual(result['result'],'COLLATERAL_RECOVERED')
        expected=copy.deepcopy(self.before);expected['halt']=None
        self.assertEqual(self.state(),expected)
        backup=Path(result['backup'])/'live.sqlite'
        with sqlite3.connect(backup) as db:
            self.assertEqual(json.loads(db.execute("SELECT json FROM state WHERE key='engine'").fetchone()[0]),self.before)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM trades').fetchone()[0],0)
        second=recovery.recover_collateral(self.root,self.api)
        self.assertEqual(second['result'],'COLLATERAL_ALREADY_CLEAR')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)

    def test_other_top_level_halt_is_never_cleared(self):
        for halt in ('MAX_DRAWDOWN','ACCOUNT_BORROWING_PRESENT','LEGACY_HALT: '+recovery.COLLATERAL_HALT):
            state=copy.deepcopy(self.before);state['halt']=halt;self.write_state(state)
            with self.assertRaisesRegex(SafetyError,'DIFFERENT_HALT'):recovery.recover_collateral(self.root,self.api)
            self.assertEqual(self.state(),state)

    def test_account_change_or_open_position_or_order_preserves_halt(self):
        for kind in ('identity','position','order'):
            self.uid='other' if kind=='identity' else 'owned-account'
            self.positions=[{}] if kind=='position' else []
            self.orders=[{}] if kind=='order' else []
            with self.subTest(kind=kind),self.assertRaises(SafetyError):recovery.recover_collateral(self.root,self.api)
            self.assertEqual(self.state(),self.before)

    def test_second_snapshot_cannot_hide_reappearing_order(self):
        original=self.snapshot()
        with patch('basic_core.account.inventory',side_effect=[original,{**original,'orders':[{}]}]):
            with self.assertRaisesRegex(SafetyError,'ACCOUNT_NOT_FLAT'):recovery.recover_collateral(self.root,self.api)
        self.assertEqual(self.state(),self.before)

    def test_active_authorization_local_position_or_pending_blocks(self):
        arm=self.root/'data/LIVE_ENABLED.json';arm.write_text('{}')
        with self.assertRaisesRegex(SafetyError,'PAUSE_NEW'):recovery.recover_collateral(self.root,self.api)
        arm.unlink()
        for key in ('position','pending'):
            state=copy.deepcopy(self.before);state['legs']['BTCUSDT'][key]={'id':'occupied'}
            self.write_state(state)
            with self.assertRaisesRegex(SafetyError,'LOCAL_POSITION'):recovery.recover_collateral(self.root,self.api)
            self.assertEqual(self.state(),state)

    def test_oversized_dust_or_insufficient_usdt_blocks_recovery(self):
        self.assets['assets'][1]['usdValue']='2'
        with self.assertRaisesRegex(SafetyError,'NON_USDT'):recovery.recover_collateral(self.root,self.api)
        self.assets={'effEquity':'625.25','imr':'0','assets':[asset(quantity=624.5,value=624.5),asset('HYPE',.01,.75)]}
        with self.assertRaisesRegex(SafetyError,'INSUFFICIENT_910'):recovery.recover_collateral(self.root,self.api)
        self.assertEqual(self.state(),self.before)

    def test_audit_insert_failure_rolls_back_halt_clear(self):
        with sqlite3.connect(self.path) as db:db.execute('DROP TABLE events')
        with self.assertRaises(sqlite3.OperationalError):recovery.recover_collateral(self.root,self.api)
        self.assertEqual(self.state(),self.before)

    def test_concurrent_engine_change_is_not_overwritten(self):
        original=self.snapshot
        changed=copy.deepcopy(self.before);changed['account_guard']['peak_equity']=1600
        calls=[]
        def snapshot(*args):
            calls.append(1)
            if len(calls)==2:self.write_state(changed)
            return original()
        with patch('basic_core.account.inventory',side_effect=snapshot):
            with self.assertRaisesRegex(SafetyError,'STATE_CHANGED'):recovery.recover_collateral(self.root,self.api)
        self.assertEqual(self.state(),changed)


if __name__=='__main__':unittest.main()
