"""Offline behavioral checks for concise Korean Telegram delivery."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from basic_core import notify
from basic_core.store import Store


class FakeResponse:
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self):return b'{"ok":true}'


class KoreanNotificationTests(unittest.TestCase):
    def test_combined_status_is_korean_contains_both_assets_and_zero_trade_reason(self):
        at=1_780_287_600_000
        state={'at':at,'account_equity':910.25,'entry_enabled':True,'qualifying_state_count':0,
               'allocation_each':400,'target_notional_each':2000,
               'legs':{'BTCUSDT':{'position':None},'ETHUSDT':{'position':{'side':'SHORT','entry':3000,'qty':.1,'stop':3020}}},
               'trends':{'BTCUSDT':{'wait_reason':'NO_COMPLETED_PRICE_VOLUME_EVENT'},
                         'ETHUSDT':{'wait_reason':'UNCERTAIN_OR_NEGATIVE_NET_EDGE'}}}
        with patch.object(notify,'now_ms',return_value=at):
            message=notify.render('HEARTBEAT',state)
        for required in ('상황 요약','910.25 USDT','각 400 USDT / 교차 5배 / 목표 포지션 각 2,000 USDT','진입 가능 상태: 0개',
                         '신규 주문을 내지 않습니다','BTCUSDT: 보유 없음','ETHUSDT: 매도',
                         '완료된 5분봉 진입 패턴 없음','비용 포함 기대값 기준 미달','KST'):
            self.assertIn(required,message)
        self.assertNotIn('UNCERTAIN_OR_NEGATIVE_NET_EDGE',message)
        self.assertNotIn('HEARTBEAT',message)

    def test_entry_and_exit_alerts_use_korean_with_event_clock_and_observed_values(self):
        stamp=1_780_287_600_000
        entry=notify.render('OPEN',{'symbol':'BTCUSDT','side':'LONG','qty':.08,'entry':30000,
                    'stop':29900,'target':30200,'initial_risk_usdt':14,
                    'event_type':'sweep_reclaim','probability':{'n_group':500,'n_state':100,
                    'p_mean':.7,'p05':.6,'break_even':.4,'lower95':.1}},event_ms=stamp)
        for required in ('진입 체결','KST','BTCUSDT 매수','진입가 30000','손절가 29900',
                         '목표가 30200','계획손실 14.00','진입 근거: 이전 고점·저점 이탈 후 복귀',
                         '그룹 500 / 상태 100','손익분기 0.4'):
            self.assertIn(required,entry)
        self.assertNotIn('OPEN',entry)
        exit_text=notify.render('CLOSE',{'symbol':'BTCUSDT','net_usdt':-7.36,
                                         'funding_usdt':-.01,'reason':'STOP'},event_ms=stamp)
        self.assertIn('청산 확인',exit_text)
        self.assertIn('정산 순손익 -7.36 USDT',exit_text)
        self.assertIn('청산 사유: 손절',exit_text)
        self.assertNotIn('CLOSE',exit_text)

    def test_summary_rate_limit_survives_restart_and_no_startup_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            filename=Path(directory)/'live.sqlite';db=Store(filename)
            status={'entry_enabled':False,'qualifying_state_count':0}
            with patch.object(notify,'now_ms',return_value=10_000):
                notify.enqueue(db,'RUNNING',{})
                notify.enqueue(db,'HEARTBEAT',status)
                notify.enqueue(db,'HEARTBEAT',status)
            db.close();db=Store(filename)
            with patch.object(notify,'now_ms',return_value=10_000+notify.SIX_HOURS_MS-1):
                notify.enqueue(db,'HEARTBEAT',status)
            self.assertEqual(db.db.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0],1)
            with db.db:db.db.execute('UPDATE telegram_outbox SET delivered=10001')
            with patch.object(notify,'now_ms',return_value=10_000+notify.SIX_HOURS_MS):
                notify.enqueue(db,'HEARTBEAT',status)
            self.assertEqual(db.db.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0],2)
            db.close()

    def test_repeated_diagnostics_are_sparse_and_urgent_events_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Store(Path(directory)/'live.sqlite')
            with patch.object(notify,'now_ms',return_value=10_000):
                notify.enqueue(db,'QUOTE_UNAVAILABLE',{'symbol':'BTCUSDT','reason':'stale quote'})
            with patch.object(notify,'now_ms',return_value=20_000):
                notify.enqueue(db,'CANDLES_UNAVAILABLE',{'symbol':'ETHUSDT','reason':'no bar'})
                notify.enqueue(db,'OPEN',{'symbol':'BTCUSDT','side':'LONG'},event_id=1,event_ms=20_000)
                notify.enqueue(db,'OPEN',{'symbol':'BTCUSDT','side':'LONG'},event_id=2,event_ms=20_000)
                notify.enqueue(db,'CLOSE',{'symbol':'BTCUSDT','reason':'STOP'},event_id=3,event_ms=20_000)
                notify.enqueue(db,'HALT',{'reason':'MAX_DRAWDOWN'},event_id=4,event_ms=20_000)
            self.assertEqual(db.db.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0],5)
            with db.db:db.db.execute("UPDATE telegram_outbox SET delivered=20001 WHERE text LIKE '%시세 확인 지연%'")
            with patch.object(notify,'now_ms',return_value=10_000+notify.SIX_HOURS_MS):
                notify.enqueue(db,'CANDLES_UNAVAILABLE',{'symbol':'ETHUSDT','reason':'no bar'})
            self.assertEqual(db.db.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0],6)
            db.close()

    def test_outage_replaces_unsent_summary_instead_of_bursting_old_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Store(Path(directory)/'live.sqlite')
            with patch.object(notify,'now_ms',return_value=10_000):
                notify.enqueue(db,'HEARTBEAT',{'qualifying_state_count':0,'entry_enabled':False})
            with patch.object(notify,'now_ms',return_value=10_000+notify.SIX_HOURS_MS):
                notify.enqueue(db,'HEARTBEAT',{'qualifying_state_count':0,'entry_enabled':True})
            with patch.object(notify,'now_ms',return_value=10_000+2*notify.SIX_HOURS_MS):
                notify.enqueue(db,'HEARTBEAT',{'qualifying_state_count':0,'entry_enabled':True,'account_equity':1110})
            rows=db.db.execute('SELECT text,attempts,next_attempt FROM telegram_outbox').fetchall()
            self.assertEqual(len(rows),1)
            self.assertIn('1,110.00 USDT',rows[0][0])
            self.assertIn('신규 진입 설정: 허용',rows[0][0])
            self.assertEqual(rows[0][1:],(0,0))
            db.close()

    def test_repeated_urgent_exit_failures_warn_now_then_at_most_every_fifteen_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Store(Path(directory)/'live.sqlite')
            kind='URGENT_EXIT_REJECTED';data={'symbol':'BTCUSDT','action':'inspect exchange'}
            with patch.object(notify,'now_ms',return_value=10_000):notify.enqueue(db,kind,data)
            with patch.object(notify,'now_ms',return_value=11_000):notify.enqueue(db,kind,data)
            self.assertEqual(db.db.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0],1)
            with db.db:db.db.execute('UPDATE telegram_outbox SET delivered=12000')
            with patch.object(notify,'now_ms',return_value=10_000+15*60_000):notify.enqueue(db,kind,data)
            self.assertEqual(db.db.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0],2)
            db.close()

    def test_old_outage_replay_is_not_delivered_as_current_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'live.sqlite')
            with patch('basic_core.store.now_ms',return_value=1_000):
                store.event('QUOTE_UNAVAILABLE',{'symbol':'BTCUSDT','reason':'old'})
                store.event('OPEN',{'symbol':'BTCUSDT','side':'LONG'})
            notify.initialize(store.db)
            recorded=[]
            box=notify.Outbox(Path(directory)/'live.sqlite',{'token':'dummy','chat':'one-chat'},
                              opener=lambda request,timeout:(recorded.append(json.loads(request.data)) or FakeResponse()))
            with patch.object(notify,'now_ms',return_value=1_000+notify.SIX_HOURS_MS):
                self.assertTrue(box.step(store.db))
            self.assertEqual(len(recorded),1)
            self.assertIn('진입 체결',recorded[0]['text'])
            self.assertEqual(recorded[0]['chat_id'],'one-chat')
            self.assertFalse(box.step(store.db,now=1_000+notify.SIX_HOURS_MS+1))
            store.close()

    def test_preview_identifies_stale_and_stopped_status_without_private_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            (root/'data/live_status.json').write_text(json.dumps({'at':1_000,'entry_enabled':False,
                   'stopped':True,'qualifying_state_count':0,'legs':{},'trends':{}}))
            with patch.object(notify,'now_ms',return_value=1_000+200_000):
                text=notify.summary(root)
            self.assertIn('관리 프로세스는 종료된 상태',text)
            self.assertIn('BTCUSDT: 보유 없음',text)
            self.assertIn('ETHUSDT: 보유 없음',text)

    def test_unknown_exchange_error_string_is_not_forwarded_to_telegram(self):
        message=notify.render('HALT',{'reason':'unexpected exchange text: TOKEN=my-private-value',
                                      'api_key':'my-private-value'})
        self.assertIn('원인 확인 필요 (서버 로그 참조)',message)
        self.assertNotIn('my-private-value',message)

    def test_manual_notify_now_sends_one_combined_message_to_one_chat_without_orders(self):
        from basic_core import cli
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            (root/'data/live_status.json').write_text(json.dumps({'at':notify.now_ms(),
                        'entry_enabled':False,'qualifying_state_count':0,'legs':{},'trends':{}}))
            with patch('basic_core.notify.credentials',return_value={'token':'dummy','chat':'one-chat'}),\
                 patch('basic_core.notify.send') as send:
                self.assertEqual(cli.main(['notify-now','--root',str(root)]),0)
            send.assert_called_once()
            self.assertIn('BTCUSDT',send.call_args.args[1])
            self.assertIn('ETHUSDT',send.call_args.args[1])
            self.assertEqual(send.call_args.args[0]['chat'],'one-chat')


if __name__=='__main__':unittest.main()
