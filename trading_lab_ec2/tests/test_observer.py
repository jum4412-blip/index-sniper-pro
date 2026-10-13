import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import observer as o

NOW = 1_800_000_120_000  # Synthetic test time; not a current API response.


def ticker(symbol='BTCUSDT', **overrides):
    row = {'symbol': symbol, 'category': 'USDT-FUTURES', 'lastPrice': '80000',
           'bid1Price': '79999', 'ask1Price': '80001', 'ts': str(NOW),
           'fundingRate': '-0.0001', 'openInterest': '100', 'markPrice': '80000'}
    row.update(overrides)
    return {'code': '00000', 'requestTime': NOW, 'data': [row]}


def candles(interval='5m', count=3):
    d = o.DURATIONS[interval]
    boundary = (NOW // d) * d
    return {'code': '00000', 'requestTime': NOW,
            'data': [[str(boundary - i*d), '100', '110', '90', '105', '1', '105']
                     for i in range(count, -1, -1)]}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'config.json'
        self.c = copy.deepcopy(o.DEFAULT_CONFIG)

    def tearDown(self):
        self.temp.cleanup()

    def load(self):
        self.path.write_text(json.dumps(self.c))
        return o.load_config(self.path)

    def test_default_valid(self):
        self.assertEqual(self.load()['mode'], 'OBSERVE_ONLY')

    def test_live_rejected(self):
        self.c['mode'] = 'LIVE'
        with self.assertRaises(o.DataError): self.load()

    def test_unknown_keys_rejected(self):
        self.c['api_secret'] = 'forbidden'
        with self.assertRaises(o.DataError): self.load()

    def test_duplicate_symbols_rejected(self):
        self.c['symbols'] = ['BTCUSDT', 'BTCUSDT']
        with self.assertRaises(o.DataError): self.load()

    def test_other_symbols_rejected(self):
        self.c['symbols'] = ['DOGEUSDT']
        with self.assertRaises(o.DataError): self.load()

    def test_weekly_not_faked(self):
        self.c['intervals'] = ['1W']
        with self.assertRaises(o.DataError): self.load()

    def test_boolean_not_valid_integer(self):
        self.c['poll_seconds'] = True
        with self.assertRaises(o.DataError): self.load()

    def test_too_fast_poll_rejected(self):
        self.c['poll_seconds'] = 1
        with self.assertRaises(o.DataError): self.load()

    def test_missing_config_key_rejected(self):
        del self.c['min_free_mb']
        with self.assertRaises(o.DataError): self.load()


class ValidationTests(unittest.TestCase):
    def setUp(self): self.cfg = copy.deepcopy(o.DEFAULT_CONFIG)

    def test_valid_ticker_negative_funding(self):
        self.assertEqual(o.validate_ticker(ticker(), 'BTCUSDT', NOW, self.cfg)['fundingRate'], '-0.0001')

    def test_nan_price_rejected(self):
        with self.assertRaises(o.DataError): o.validate_ticker(ticker(lastPrice='NaN'), 'BTCUSDT', NOW, self.cfg)

    def test_infinity_rejected(self):
        with self.assertRaises(o.DataError): o.number('Infinity')

    def test_boolean_rejected(self):
        with self.assertRaises(o.DataError): o.number(True)

    def test_fractional_timestamp_rejected(self):
        with self.assertRaises(o.DataError): o.integer('1.5')

    def test_wrong_market_rejected(self):
        with self.assertRaises(o.DataError): o.validate_ticker(ticker(category='SPOT'), 'BTCUSDT', NOW, self.cfg)

    def test_wrong_symbol_rejected(self):
        with self.assertRaises(o.DataError): o.validate_ticker(ticker('ETHUSDT'), 'BTCUSDT', NOW, self.cfg)

    def test_stale_ticker_rejected(self):
        with self.assertRaises(o.DataError): o.validate_ticker(ticker(ts=str(NOW-190000)), 'BTCUSDT', NOW, self.cfg)

    def test_future_ticker_rejected(self):
        with self.assertRaises(o.DataError): o.validate_ticker(ticker(ts=str(NOW+10000)), 'BTCUSDT', NOW, self.cfg)

    def test_clock_skew_rejected(self):
        with self.assertRaises(o.DataError): o.validate_ticker(ticker(), 'BTCUSDT', NOW+130000, self.cfg)

    def test_crossed_book_rejected(self):
        with self.assertRaises(o.DataError): o.validate_ticker(ticker(bid1Price='90000'), 'BTCUSDT', NOW, self.cfg)

    def test_missing_funding_not_zero_filled(self):
        row = o.validate_ticker(ticker(fundingRate=''), 'BTCUSDT', NOW, self.cfg)
        self.assertEqual(row['fundingRate'], '')

    def test_only_closed_candles_stored(self):
        rows = o.validate_candles(candles(), '5m', NOW, self.cfg)
        self.assertTrue(rows)
        self.assertTrue(all(r[0]+300000+5000 <= NOW for r in rows))
        self.assertEqual(len(rows), 3)

    def test_candle_duplicate_deduplicated(self):
        p = candles()
        p['data'].append(p['data'][0].copy())
        self.assertEqual(len(o.validate_candles(p, '5m', NOW, self.cfg)), 3)

    def test_conflicting_candle_duplicate_rejected(self):
        p = candles()
        r = p['data'][0].copy(); r[4] = '106'; p['data'].append(r)
        with self.assertRaises(o.DataError): o.validate_candles(p, '5m', NOW, self.cfg)

    def test_invalid_ohlc_rejected(self):
        p = candles(); p['data'][0][2] = '90'
        with self.assertRaises(o.DataError): o.validate_candles(p, '5m', NOW, self.cfg)

    def test_negative_volume_rejected(self):
        p = candles(); p['data'][0][5] = '-1'
        with self.assertRaises(o.DataError): o.validate_candles(p, '5m', NOW, self.cfg)

    def test_unclosed_only_rejected(self):
        p = candles(count=0)
        with self.assertRaises(o.DataError): o.validate_candles(p, '5m', NOW, self.cfg)

    def test_stale_candles_rejected(self):
        p = candles()
        for r in p['data']: r[0] = str(int(r[0])-3000000)
        with self.assertRaises(o.DataError): o.validate_candles(p, '5m', NOW, self.cfg)

    def test_future_candle_rejected(self):
        p = candles(); p['data'][0][0] = str(NOW+100000)
        with self.assertRaises(o.DataError): o.validate_candles(p, '5m', NOW, self.cfg)

    def test_interval_misalignment_rejected(self):
        p = candles(); p['data'][0][0] = str(int(p['data'][0][0])+1)
        with self.assertRaises(o.DataError): o.validate_candles(p, '5m', NOW, self.cfg)

    def test_order_endpoint_blocked_before_network(self):
        with self.assertRaises(o.DataError): o.PublicClient().get('/api/v3/trade/place-order', {})

    def test_account_endpoint_blocked_before_network(self):
        with self.assertRaises(o.DataError): o.PublicClient().get('/api/v3/account/assets', {})

    def test_bad_category_blocked_before_network(self):
        with self.assertRaises(o.DataError): o.PublicClient().get('/api/v3/market/tickers', {'category':'SPOT','symbol':'BTCUSDT'})

    def test_redirect_blocked(self):
        with self.assertRaises(o.DataError): o.NoRedirect().redirect_request(None,None,302,None,{},'http://bad.example')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(o.DEFAULT_CONFIG)
        self.cfg.update({'symbols': ['BTCUSDT'], 'intervals': ['5m']})
        self.path = self.root/'shared/data/market.sqlite'
        self.s = o.Store(self.path)

    def tearDown(self):
        self.s.close(); self.temp.cleanup()

    def rows(self): return o.validate_candles(candles(), '5m', NOW, self.cfg)

    def test_quote_deduplication(self):
        row = ticker()['data'][0]
        self.s.quote('BTCUSDT', row, NOW); self.s.quote('BTCUSDT', row, NOW+1)
        self.assertEqual(self.s.db.execute('select count(*) from quotes').fetchone()[0], 1)

    def test_candle_deduplication(self):
        rows = self.rows()
        self.s.candles('BTCUSDT','5m',rows,NOW); self.s.candles('BTCUSDT','5m',rows,NOW+1000)
        self.assertEqual(self.s.coverage(self.cfg)[0]['count'], 3)
        self.assertEqual(self.s.db.execute('select count(*) from candle_revisions').fetchone()[0], 0)

    def test_revision_is_audited(self):
        rows=self.rows(); self.s.candles('BTCUSDT','5m',rows,NOW)
        rows[0][4]='106'; self.s.candles('BTCUSDT','5m',rows,NOW+1)
        audit=self.s.db.execute('select old_payload,new_payload from candle_revisions').fetchone()
        self.assertEqual(json.loads(audit[0])[4],'105')
        self.assertEqual(json.loads(audit[1])[4],'106')
        self.assertEqual(self.s.db.execute('select first_seen_ms from candles order by open_ms').fetchone()[0], NOW)

    def test_gap_detected_not_filled(self):
        rows=self.rows(); self.s.candles('BTCUSDT','5m',[rows[0],rows[2]],NOW)
        self.assertEqual(self.s.coverage(self.cfg)[0]['missing_bars_in_stored_range'],1)

    def test_candle_phase_changes_rejected_across_cycles(self):
        rows=self.rows(); self.s.candles('BTCUSDT','5m',rows,NOW)
        shifted=[r.copy() for r in rows]
        for r in shifted: r[0] += 1
        with self.assertRaises(o.DataError): self.s.candles('BTCUSDT','5m',shifted,NOW+1000)
        self.assertEqual(self.s.coverage(self.cfg)[0]['count'],3)

    def test_backup_contains_valid_sqlite(self):
        self.s.candles('BTCUSDT','5m',self.rows(),NOW)
        result=o.backup(self.root,self.cfg)
        self.assertFalse(result['off_instance_backup'])
        with sqlite3.connect(Path(result['path'])/'market.sqlite') as db:
            self.assertEqual(db.execute('select count(*) from candles').fetchone()[0],3)
        self.assertTrue((Path(result['path'])/'manifest.json').exists())

    def test_backup_rotation_keeps_seven_complete_snapshots(self):
        self.s.candles('BTCUSDT','5m',self.rows(),NOW)
        for _ in range(9): o.backup(self.root,self.cfg)
        folders=list((self.root/'shared/backups').glob('snapshot-*'))
        self.assertEqual(len(folders),7)
        self.assertTrue(all((p/'manifest.json').exists() for p in folders))

    def test_reopen_preserves_data(self):
        self.s.candles('BTCUSDT','5m',self.rows(),NOW)
        s2=o.Store(self.path)
        try: self.assertEqual(s2.coverage(self.cfg)[0]['count'],3)
        finally: s2.close()

    def test_schema_mismatch_blocked(self):
        with self.s.db: self.s.db.execute("update metadata set v='999' where k='schema'")
        with self.assertRaises(o.DataError): o.Store(self.path)

    def test_notifications_persist(self):
        self.s.mark_notification('daily','2026-10-13')
        self.assertEqual(self.s.notification('daily'),'2026-10-13')

    def test_full_cycle_fake_api(self):
        class Client:
            def get(self, path, params):
                return ticker() if path.endswith('/tickers') else candles(params['interval'])
        with patch('observer.now_ms', return_value=NOW):
            result=o.cycle(self.root,self.cfg,self.s,Client())
        self.assertEqual(result['health'],'OK')
        self.assertFalse(result['order_submission'])
        self.assertFalse(result['account_connected'])
        self.assertFalse(result['strategy_signals_implemented'])
        self.assertEqual(result['coverage'][0]['count'],3)

    def test_failed_cycle_preserves_last_success(self):
        class Client:
            def get(self,path,params): raise o.DataError('network_or_timeout_error')
        with patch('observer.now_ms', return_value=NOW):
            result=o.cycle(self.root,self.cfg,self.s,Client(),{'last_success_ms':NOW-1000})
        self.assertEqual(result['health'],'API_OR_DATA_ERROR')
        self.assertEqual(result['last_success_ms'],NOW-1000)
        self.assertEqual(len(result['errors']),2)

    def test_stale_status_not_healthy(self):
        o.atomic_json(self.root/'shared/status.json', {'health':'OK','updated_ms':NOW-400000})
        with patch('observer.now_ms',return_value=NOW): report,code=o.status_report(self.root,self.cfg)
        self.assertEqual(code,2); self.assertEqual(report['health'],'STALE_OR_CLOCK_ERROR')

    def test_not_started(self):
        report,code=o.status_report(self.root,self.cfg)
        self.assertEqual(code,2); self.assertEqual(report['health'],'NOT_STARTED')

    def test_double_collector_lock_rejected(self):
        with o.exclusive_lock(self.root/'lock'):
            with self.assertRaises(o.DataError):
                with o.exclusive_lock(self.root/'lock'): pass

    def test_atomic_json_permissions(self):
        p=self.root/'x.json'; o.atomic_json(p,{'x':1})
        self.assertEqual(p.stat().st_mode & 0o777,0o600)
        self.assertEqual(json.loads(p.read_text()),{'x':1})


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)/'telegram.env'

    def tearDown(self): self.temp.cleanup()

    def write(self,text,mode=0o600): self.path.write_text(text); self.path.chmod(mode)

    def test_absent_optional(self): self.assertIsNone(o.read_telegram(self.path))

    def test_empty_optional(self):
        self.write('TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n'); self.assertIsNone(o.read_telegram(self.path))

    def test_wrong_permissions_rejected(self):
        self.write('',0o644)
        with self.assertRaises(o.DataError): o.read_telegram(self.path)

    def test_other_bot_secrets_not_accepted(self):
        self.write('BITGET_API_SECRET=DO_NOT_LOAD\n')
        with self.assertRaises(o.DataError): o.read_telegram(self.path)

    def test_shell_commands_not_executed(self):
        self.write('TELEGRAM_BOT_TOKEN=$(touch SHOULD_NOT_EXIST)\nTELEGRAM_CHAT_ID=123\n')
        with self.assertRaises(o.DataError): o.read_telegram(self.path)
        self.assertFalse((Path(self.temp.name)/'SHOULD_NOT_EXIST').exists())

    def test_valid_format(self):
        self.write('TELEGRAM_BOT_TOKEN=12345678:'+'x'*30+'\nTELEGRAM_CHAT_ID=-100123456\n')
        creds=o.read_telegram(self.path); self.assertEqual(creds[1],'-100123456')

    def test_delivery_error_does_not_leak_token(self):
        token='12345678:'+'x'*30
        with patch('urllib.request.build_opener',side_effect=RuntimeError('https://api.telegram.org/bot'+token)):
            with self.assertRaisesRegex(o.DataError,'^telegram_delivery_failed$'):
                o.send_telegram((token,'1'),'test')


if __name__ == '__main__': unittest.main()
