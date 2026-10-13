#!/usr/bin/env python3
"""Bitget public-only collector. NO exchange signing, account access or orders.

Standard library only, Python >= 3.10, Linux for the exclusive process lock.
Freshness is separate from process liveness. Raw source timestamps are retained.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = '0.2.0-observe'
SCHEMA = 1
CATEGORY = 'USDT-FUTURES'
BASE = 'https://api.bitget.com'
ALLOWED_PATHS = frozenset(('/api/v3/market/tickers', '/api/v3/market/candles'))
DURATIONS = {'5m': 300000, '1H': 3600000, '4H': 14400000, '1D': 86400000}
DEFAULT_CONFIG = {
    'schema_version': 1, 'mode': 'OBSERVE_ONLY',
    'symbols': ['BTCUSDT', 'ETHUSDT'], 'intervals': ['5m', '1H', '4H', '1D'],
    'poll_seconds': 60, 'http_timeout_seconds': 10, 'candle_limit': 200,
    'close_grace_ms': 5000, 'clock_skew_limit_ms': 120000,
    'min_free_mb': 512, 'quote_retention_days': 30, 'stale_seconds': 300,
}
STOP = threading.Event()


class DataError(ValueError):
    pass


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def now_ms() -> int:
    return time.time_ns() // 1000000


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def number(value, *, positive=False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise DataError('missing_or_invalid_number')
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DataError('invalid_number') from None
    if not n.is_finite() or (positive and n <= 0):
        raise DataError('nonfinite_or_nonpositive_number')
    return n


def integer(value, *, positive=True) -> int:
    n = number(value, positive=positive)
    if n != n.to_integral_value():
        raise DataError('noninteger_number')
    return int(n)


def load_config(path: Path) -> dict:
    c = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(c, dict) or set(c) != set(DEFAULT_CONFIG):
        raise DataError('configuration_keys_mismatch')
    if c['schema_version'] != 1 or isinstance(c['schema_version'], bool):
        raise DataError('configuration_schema_mismatch')
    if c['mode'] != 'OBSERVE_ONLY':
        raise DataError('only_OBSERVE_ONLY_is_implemented')
    for key, allowed in [('symbols', {'BTCUSDT', 'ETHUSDT'}), ('intervals', set(DURATIONS))]:
        v = c[key]
        if (not isinstance(v, list) or not v or not all(isinstance(x, str) for x in v)
                or len(set(v)) != len(v) or not set(v) <= allowed):
            raise DataError('invalid_' + key)
    ranges = {'poll_seconds': (30, 300), 'http_timeout_seconds': (2, 15),
              'candle_limit': (20, 1000), 'close_grace_ms': (1000, 60000),
              'clock_skew_limit_ms': (1000, 120000), 'min_free_mb': (128, 10240),
              'quote_retention_days': (1, 365), 'stale_seconds': (180, 1800)}
    for key, (lo, hi) in ranges.items():
        if type(c[key]) is not int or not lo <= c[key] <= hi:
            raise DataError('invalid_' + key)
    if c['stale_seconds'] < c['poll_seconds'] + 120:
        raise DataError('stale_seconds_too_short')
    return c


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    try:
        with open(temp, 'w', encoding='utf-8') as fh:
            os.chmod(temp, 0o600)
            fh.write(dumps(data) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temp.exists():
            temp.unlink()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError('http_redirect_rejected')


class PublicClient:
    def __init__(self, timeout=10):
        self.timeout = timeout
        # No proxies or authentication from another bot's environment/config.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(self, path: str, params: dict) -> dict:
        if path not in ALLOWED_PATHS:
            raise DataError('public_endpoint_not_allowed')
        if params.get('category') != CATEGORY or params.get('symbol') not in {'BTCUSDT', 'ETHUSDT'}:
            raise DataError('invalid_public_request')
        url = BASE + path + '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'lab1030-public-observer/' + VERSION,
                                                   'Accept': 'application/json'}, method='GET')
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise DataError('response_too_large')
            result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise DataError('http_status_' + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DataError('network_or_timeout_error') from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DataError('invalid_response_json') from None
        if not isinstance(result, dict):
            raise DataError('response_not_object')
        if result.get('code') != '00000':
            code = str(result.get('code', 'missing'))
            raise DataError('exchange_error_' + (code if re.fullmatch(r'[A-Za-z0-9_-]{1,24}', code) else 'unknown'))
        return result


def server_time(payload: dict, received_ms: int, cfg: dict) -> int:
    if payload.get('code') != '00000':
        raise DataError('unsuccessful_response')
    ts = integer(payload.get('requestTime'))
    if abs(ts - received_ms) > cfg['clock_skew_limit_ms']:
        raise DataError('server_local_clock_skew')
    return ts


def validate_ticker(payload: dict, symbol: str, received_ms: int, cfg: dict) -> dict:
    st = server_time(payload, received_ms, cfg)
    rows = payload.get('data')
    if not isinstance(rows, list):
        raise DataError('ticker_list_missing')
    matches = [r for r in rows if isinstance(r, dict) and r.get('symbol') == symbol]
    if len(matches) != 1:
        raise DataError('ticker_symbol_missing_or_duplicate')
    row = matches[0]
    if row.get('category') != CATEGORY:
        raise DataError('ticker_category_mismatch')
    number(row.get('lastPrice'), positive=True)
    ts = integer(row.get('ts'))
    if ts > st + 5000 or st - ts > 180000:
        raise DataError('ticker_timestamp_not_fresh')
    bid = row.get('bid1Price')
    ask = row.get('ask1Price')
    if bid not in (None, '') and ask not in (None, ''):
        if number(bid, positive=True) > number(ask, positive=True):
            raise DataError('crossed_bid_ask')
    for name in ('fundingRate', 'openInterest', 'markPrice'):
        v = row.get(name)
        if v not in (None, ''):
            n = number(v, positive=(name == 'markPrice'))
            if name == 'openInterest' and n < 0:
                raise DataError('negative_open_interest')
    return row


def validate_candles(payload: dict, interval: str, received_ms: int, cfg: dict) -> list[list]:
    st = server_time(payload, received_ms, cfg)
    rows = payload.get('data')
    if interval not in DURATIONS or not isinstance(rows, list) or not rows:
        raise DataError('candles_missing_or_interval_invalid')
    duration = DURATIONS[interval]
    seen = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            raise DataError('candle_shape_invalid')
        ts = integer(row[0])
        if ts > st + 5000:
            raise DataError('future_candle')
        vals = [number(x, positive=(i < 4)) for i, x in enumerate(row[1:7])]
        o, h, l, c, bv, qv = vals
        if h < max(o, c) or l > min(o, c) or h < l or bv < 0 or qv < 0:
            raise DataError('candle_ohlcv_invalid')
        normalized = [ts] + [str(v) for v in vals]
        if ts in seen and seen[ts] != normalized:
            raise DataError('conflicting_candle_duplicate')
        seen[ts] = normalized
    ordered = sorted(seen)
    if any((b-a) % duration != 0 for a, b in zip(ordered, ordered[1:])):
        raise DataError('candle_interval_misaligned')
    closed = [seen[t] for t in ordered if t + duration + cfg['close_grace_ms'] <= st]
    if not closed:
        raise DataError('no_closed_candles')
    if st - (closed[-1][0] + duration) > duration + cfg['stale_seconds'] * 1000:
        raise DataError('latest_closed_candle_is_stale')
    return closed


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata(k TEXT PRIMARY KEY,v TEXT NOT NULL)')
        row = self.db.execute("SELECT v FROM metadata WHERE k='schema'").fetchone()
        if row and row[0] != str(SCHEMA):
            self.db.close()
            raise DataError('database_schema_mismatch')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS quotes(symbol TEXT,market_ts INTEGER,seen_ms INTEGER,payload TEXT,
          PRIMARY KEY(symbol,market_ts));
        CREATE TABLE IF NOT EXISTS candles(symbol TEXT,interval TEXT,open_ms INTEGER,payload TEXT,
          first_seen_ms INTEGER,last_seen_ms INTEGER,PRIMARY KEY(symbol,interval,open_ms));
        CREATE TABLE IF NOT EXISTS candle_revisions(id INTEGER PRIMARY KEY,symbol TEXT,interval TEXT,
          open_ms INTEGER,seen_ms INTEGER,old_payload TEXT,new_payload TEXT);
        CREATE TABLE IF NOT EXISTS cycles(id INTEGER PRIMARY KEY,seen_ms INTEGER,summary TEXT);
        CREATE TABLE IF NOT EXISTS notifications(k TEXT PRIMARY KEY,v TEXT NOT NULL);
        ''')
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES('schema',?)", (str(SCHEMA),))
        self.db.commit()
        os.chmod(path, 0o600)

    def close(self):
        self.db.close()

    def quote(self, symbol: str, row: dict, seen_ms: int):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO quotes VALUES(?,?,?,?)',
                            (symbol, integer(row['ts']), seen_ms, dumps(row)))

    def candles(self, symbol: str, interval: str, rows: list[list], seen_ms: int):
        with self.db:
            anchor = self.db.execute('SELECT MIN(open_ms) FROM candles WHERE symbol=? AND interval=?', (symbol, interval)).fetchone()[0]
            if anchor is not None and any((r[0]-anchor) % DURATIONS[interval] != 0 for r in rows):
                raise DataError('stored_candle_phase_mismatch')
            for row in rows:
                key = (symbol, interval, row[0])
                payload = dumps(row)
                old = self.db.execute('SELECT payload FROM candles WHERE symbol=? AND interval=? AND open_ms=?', key).fetchone()
                if old is None:
                    self.db.execute('INSERT INTO candles VALUES(?,?,?,?,?,?)', (*key, payload, seen_ms, seen_ms))
                else:
                    if old[0] != payload:
                        self.db.execute('INSERT INTO candle_revisions(symbol,interval,open_ms,seen_ms,old_payload,new_payload) VALUES(?,?,?,?,?,?)',
                                        (*key, seen_ms, old[0], payload))
                    self.db.execute('UPDATE candles SET payload=?,last_seen_ms=? WHERE symbol=? AND interval=? AND open_ms=?',
                                    (payload, seen_ms, *key))

    def coverage(self, cfg: dict) -> list[dict]:
        out = []
        for symbol in cfg['symbols']:
            for interval in cfg['intervals']:
                count, start, end = self.db.execute('SELECT COUNT(*),MIN(open_ms),MAX(open_ms) FROM candles WHERE symbol=? AND interval=?',
                                                   (symbol, interval)).fetchone()
                missing = ((end-start)//DURATIONS[interval] + 1-count) if count else 0
                out.append({'symbol': symbol, 'interval': interval, 'count': count,
                            'first_open_ms': start, 'last_open_ms': end, 'missing_bars_in_stored_range': missing})
        return out

    def record(self, status: dict, retention_days: int):
        with self.db:
            self.db.execute('INSERT INTO cycles(seen_ms,summary) VALUES(?,?)', (status['updated_ms'], dumps(status)))
            cutoff = status['updated_ms'] - retention_days * 86400000
            self.db.execute('DELETE FROM quotes WHERE seen_ms < ?', (cutoff,))
            self.db.execute('DELETE FROM cycles WHERE seen_ms < ?', (status['updated_ms'] - 30 * 86400000,))

    def notification(self, key: str):
        row = self.db.execute('SELECT v FROM notifications WHERE k=?', (key,)).fetchone()
        return row[0] if row else None

    def mark_notification(self, key: str, value: str):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO notifications VALUES(?,?)', (key, value))


def sd_notify(message: str):
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError:
        pass


@contextmanager
def exclusive_lock(path: Path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DataError('collector_already_running') from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        yield


def read_telegram(path: Path):
    if not path.exists():
        return None
    if path.is_symlink() or (path.stat().st_mode & 0o077):
        raise DataError('telegram_file_requires_permissions_600')
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise DataError('telegram_file_format_invalid')
        k, v = line.split('=', 1)
        if k not in {'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'} or k in values:
            raise DataError('telegram_file_key_invalid')
        values[k] = v.strip()
    token, chat = values.get('TELEGRAM_BOT_TOKEN', ''), values.get('TELEGRAM_CHAT_ID', '')
    if not token and not chat:
        return None
    if not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,100}', token) or not re.fullmatch(r'-?[0-9]{1,25}', chat):
        raise DataError('telegram_credentials_format_invalid')
    return token, chat


def send_telegram(creds, text: str):
    token, chat = creds
    # Token is in the request URL, but NEVER in logs/errors/output.
    req = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage',
                                 data=urllib.parse.urlencode({'chat_id': chat, 'text': text[:3800]}).encode(),
                                 method='POST')
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=8) as response:
            result = json.loads(response.read(100000))
        if result.get('ok') is not True:
            raise DataError('telegram_delivery_failed')
    except Exception:
        raise DataError('telegram_delivery_failed') from None


def maybe_notify(root: Path, store: Store, status: dict):
    creds = read_telegram(root / 'config' / 'telegram.env')
    if not creds:
        return
    dt = datetime.fromtimestamp(status['updated_ms']/1000, timezone(timedelta(hours=9)))
    day = dt.date().isoformat()
    state = status['health']
    old = store.notification('health')
    daily_due = dt.hour >= 9 and store.notification('daily') != day
    incident_due = state != 'OK' and old != state
    recovered = state == 'OK' and old not in (None, 'OK')
    last_sent = int(store.notification('sent_ms') or '0')
    # One combined notice at most each 30 minutes. State remains pending on throttling/failure.
    if not (daily_due or incident_due or recovered) or status['updated_ms'] - last_sent < 1800000:
        return
    counts = sum(r['count'] for r in status['coverage'])
    text = (f'[LAB1030 관찰 전용]\n상태: {state}\n'
            f'마감봉 저장: {counts}개\n'
            f'이번 조회 오류: {len(status["errors"])}개\n'
            '실주문: 없음 / 실제 계좌 조회: 미연결\n'
            '기존 실매매 봇의 보호 상태를 확인한 알림이 아닙니다.')
    last_attempt = int(store.notification('attempt_ms') or '0')
    if status['updated_ms'] - last_attempt < 300000:
        return
    store.mark_notification('attempt_ms', str(status['updated_ms']))
    send_telegram(creds, text)
    store.mark_notification('health', state)
    store.mark_notification('sent_ms', str(status['updated_ms']))
    if daily_due:
        store.mark_notification('daily', day)


def cycle(root: Path, cfg: dict, store: Store, client: PublicClient, previous=None) -> dict:
    previous = previous or {}
    start = now_ms()
    errors, quotes = [], {}
    for symbol in cfg['symbols']:
        if STOP.is_set():
            break
        try:
            p = client.get('/api/v3/market/tickers', {'category': CATEGORY, 'symbol': symbol})
            ts = now_ms()
            row = validate_ticker(p, symbol, ts, cfg)
            store.quote(symbol, row, ts)
            quotes[symbol] = {'lastPrice': row['lastPrice'], 'market_ts': integer(row['ts'])}
        except (DataError, KeyError, TypeError) as exc:
            errors.append({'symbol': symbol, 'kind': 'ticker', 'error': str(exc)[:160]})
        sd_notify('WATCHDOG=1')
        for interval in cfg['intervals']:
            if STOP.is_set():
                break
            try:
                p = client.get('/api/v3/market/candles', {'category': CATEGORY, 'symbol': symbol,
                                'interval': interval, 'limit': cfg['candle_limit'], 'type': 'market'})
                ts = now_ms()
                rows = validate_candles(p, interval, ts, cfg)
                store.candles(symbol, interval, rows, ts)
            except (DataError, KeyError, TypeError) as exc:
                errors.append({'symbol': symbol, 'kind': interval, 'error': str(exc)[:160]})
            sd_notify('WATCHDOG=1')
    coverage = store.coverage(cfg)
    gaps = any(r['missing_bars_in_stored_range'] > 0 for r in coverage)
    health = 'API_OR_DATA_ERROR' if errors else ('DATA_GAP_REVIEW' if gaps else 'OK')
    if STOP.is_set():
        health = 'STOPPED'
    end = now_ms()
    try:
        commit = (Path(__file__).resolve().parent / 'DEPLOY_COMMIT').read_text().strip()
    except FileNotFoundError:
        commit = 'local-uncommitted'
    status = {'mode': 'OBSERVE_ONLY', 'order_submission': False, 'account_connected': False,
              'strategy_signals_implemented': False, 'version': VERSION, 'commit': commit,
              'health': health, 'updated_ms': end, 'updated_utc': iso(end),
              'cycle_ms': end - start, 'last_success_ms': end if health == 'OK' else previous.get('last_success_ms'),
              'quotes': quotes, 'coverage': coverage, 'errors': errors,
              'history_note': 'Recent rolling windows only; gaps are not interpolated; no multi-year backfill.'}
    store.record(status, cfg['quote_retention_days'])
    atomic_json(root / 'shared' / 'status.json', status)
    return status


def check_disk(root: Path, cfg: dict):
    if shutil.disk_usage(root).free < cfg['min_free_mb'] * 1024**2:
        raise DataError('insufficient_free_disk')


def run(root: Path, cfg: dict, once=False):
    shared = root / 'shared'
    shared.mkdir(parents=True, exist_ok=True)
    logs = shared / 'logs'
    logs.mkdir(exist_ok=True)
    logger = logging.getLogger('lab1030')
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(logs / 'observer.log', maxBytes=2_000_000, backupCount=5, encoding='utf-8')
    logger.addHandler(handler)
    with exclusive_lock(shared / 'collector.lock'):
        check_disk(root, cfg)
        store = Store(shared / 'data' / 'market.sqlite')
        try:
            status_file = shared / 'status.json'
            previous = json.loads(status_file.read_text()) if status_file.exists() else {}
            client = PublicClient(cfg['http_timeout_seconds'])
            sd_notify('READY=1\nWATCHDOG=1\nSTATUS=Public collector only; account/orders not connected')
            while not STOP.is_set():
                check_disk(root, cfg)
                status = cycle(root, cfg, store, client, previous)
                try:
                    maybe_notify(root, store, status)
                except DataError as exc:
                    status['notification_error'] = str(exc)
                    atomic_json(status_file, status)
                logger.info(dumps({'time': status['updated_utc'], 'health': status['health'], 'errors': status['errors']}))
                print(dumps({'mode': 'OBSERVE_ONLY', 'health': status['health'], 'updated_utc': status['updated_utc']}), flush=True)
                previous = status
                if once:
                    return 0 if status['health'] == 'OK' else 2
                until = time.monotonic() + cfg['poll_seconds']
                while not STOP.is_set() and time.monotonic() < until:
                    sd_notify('WATCHDOG=1')
                    STOP.wait(min(10, max(0, until-time.monotonic())))
            if previous:
                previous['health'] = 'STOPPED'
                previous['updated_ms'] = now_ms()
                previous['updated_utc'] = iso(previous['updated_ms'])
                atomic_json(shared / 'status.json', previous)
            return 0
        finally:
            sd_notify('STOPPING=1')
            store.close()
            handler.close()
            logger.removeHandler(handler)


def status_report(root: Path, cfg: dict) -> tuple[dict, int]:
    path = root / 'shared' / 'status.json'
    if not path.is_file():
        return {'mode': 'OBSERVE_ONLY', 'order_submission': False, 'health': 'NOT_STARTED'}, 2
    status = json.loads(path.read_text())
    age = (now_ms() - status['updated_ms']) / 1000
    status['status_age_seconds'] = round(age, 1)
    if age < -5 or age > cfg['stale_seconds']:
        status['reported_health_before_staleness_check'] = status['health']
        status['health'] = 'STALE_OR_CLOCK_ERROR'
    status['important'] = '수집기 상태입니다. 실제 계좌·주문·보호 상태는 조회하지 않습니다.'
    return status, 0 if status['health'] == 'OK' else 2


def doctor(root: Path, cfg: dict, offline=False) -> tuple[dict, int]:
    checks = {'configuration': 'OK', 'python': sys.version.split()[0],
              'mode': 'OBSERVE_ONLY', 'order_submission': False, 'account_connected': False,
              'free_mb': shutil.disk_usage(root).free // 1024**2, 'public_endpoints': []}
    ok = checks['free_mb'] >= cfg['min_free_mb']
    try:
        checks['telegram'] = 'CONFIGURED_NOT_TESTED' if read_telegram(root / 'config' / 'telegram.env') else 'NOT_CONFIGURED'
    except DataError as exc:
        checks['telegram'] = str(exc)
        ok = False
    if not offline:
        client = PublicClient(cfg['http_timeout_seconds'])
        for symbol in cfg['symbols']:
            for kind in ['ticker'] + cfg['intervals']:
                try:
                    if kind == 'ticker':
                        p = client.get('/api/v3/market/tickers', {'category': CATEGORY, 'symbol': symbol})
                        validate_ticker(p, symbol, now_ms(), cfg)
                    else:
                        p = client.get('/api/v3/market/candles', {'category': CATEGORY, 'symbol': symbol,
                                        'interval': kind, 'limit': '20', 'type': 'market'})
                        validate_candles(p, kind, now_ms(), cfg)
                    checks['public_endpoints'].append({'symbol': symbol, 'kind': kind, 'result': 'OK'})
                except DataError as exc:
                    checks['public_endpoints'].append({'symbol': symbol, 'kind': kind, 'result': str(exc)})
                    ok = False
    else:
        checks['public_network'] = 'NOT_TESTED_OFFLINE'
    checks['result'] = 'OK' if ok else 'CHECK_FAILED'
    checks['important'] = '계좌·전략 수익성·주문 보호·기존 봇 정상 여부를 검사한 결과가 아닙니다.'
    return checks, 0 if ok else 2


def backup(root: Path, cfg: dict) -> dict:
    """Online SQLite snapshots; local copies, NOT off-instance disaster recovery."""
    shared = root / 'shared'
    source = shared / 'data'
    destination = shared / 'backups'
    destination.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(shared / 'backup.lock'):
        dbs = sorted(source.glob('*.sqlite')) if source.exists() else []
        if not dbs:
            return {'result': 'NO_DATABASE_YET', 'off_instance_backup': False}
        needed = sum(p.stat().st_size for p in dbs) * 2 + cfg['min_free_mb'] * 1024**2
        if shutil.disk_usage(root).free < needed:
            raise DataError('insufficient_disk_for_backup')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + str(time.time_ns())
        temp = destination / ('.partial-' + stamp)
        final = destination / ('snapshot-' + stamp)
        temp.mkdir(mode=0o700)
        try:
            manifest = {'created_utc': iso(now_ms()), 'off_instance_backup': False, 'files': {}}
            for p in dbs:
                if p.is_symlink():
                    raise DataError('database_symlink_rejected')
                src = sqlite3.connect(p.resolve().as_uri() + '?mode=ro', uri=True, timeout=20)
                dst = sqlite3.connect(temp / p.name)
                deadline = time.monotonic() + 60
                def progress(*args):
                    if time.monotonic() > deadline:
                        raise DataError('backup_timed_out')
                try:
                    src.backup(dst, pages=128, progress=progress, sleep=0.05)
                    if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise DataError('backup_integrity_check_failed')
                finally:
                    dst.close()
                    src.close()
                digest = hashlib.sha256()
                with open(temp / p.name, 'rb') as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                        digest.update(chunk)
                manifest['files'][p.name] = digest.hexdigest()
            atomic_json(temp / 'manifest.json', manifest)
            os.replace(temp, final)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        # Remove only successful snapshots owned by this backup routine.
        snapshots = sorted(p for p in destination.glob('snapshot-*') if p.is_dir() and (p/'manifest.json').is_file())
        for old in snapshots[:-7]:
            shutil.rmtree(old)
        return {'result': 'OK', 'path': str(final), 'database_count': len(dbs),
                'retained_snapshots': min(len(snapshots), 7), 'off_instance_backup': False}


def main():
    parser = argparse.ArgumentParser(description='EC2 public observer; NO EXCHANGE ORDERS')
    parser.add_argument('command', choices=['run', 'once', 'doctor', 'status', 'backup', 'notify-test'])
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    cfg = load_config(root / 'config' / 'observer.json')
    if args.command in {'run', 'once'}:
        signal.signal(signal.SIGTERM, lambda *_: STOP.set())
        signal.signal(signal.SIGINT, lambda *_: STOP.set())
        return run(root, cfg, once=(args.command == 'once'))
    if args.command == 'doctor':
        result, code = doctor(root, cfg, args.offline)
    elif args.command == 'status':
        result, code = status_report(root, cfg)
    elif args.command == 'backup':
        result, code = backup(root, cfg), 0
    else:
        credentials = read_telegram(root / 'config' / 'telegram.env')
        if not credentials:
            raise DataError('telegram_not_configured')
        send_telegram(credentials, '[LAB1030] EC2 관찰 수집기 테스트. 실주문·실제 계좌 조회·기존 봇 보호 검사 없음.')
        result, code = {'result': 'TELEGRAM_SENT', 'mode': 'OBSERVE_ONLY'}, 0
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (DataError, ValueError, OSError, sqlite3.Error) as exc:
        # No HTTP URLs with tokens or account credentials appear here.
        text = str(exc) if isinstance(exc, DataError) else type(exc).__name__
        print('ERROR: ' + text, file=sys.stderr)
        sys.exit(2)
