"""Audited, stopped-manager recovery of one resolved collateral-format halt."""
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from . import account, runtime
from .api import Rest, credentials
from .core import SafetyError, config, now_ms

COLLATERAL_HALT = 'NON_USDT_COLLATERAL_UNSUPPORTED'


def service_stopped(root):
    from .service import SERVICE
    path = Path('/etc/systemd/system') / SERVICE
    if not path.exists():
        return
    result = subprocess.run(['systemctl', 'show', SERVICE, '--property=ActiveState,MainPID'],
                            text=True, capture_output=True, check=True, timeout=15)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if values.get('ActiveState') not in ('inactive', 'failed') or values.get('MainPID') != '0':
        raise SafetyError('STOP_NEW_SERVICE_BEFORE_COLLATERAL_RECOVERY')


def cleared_state(state):
    from .cli import occupied
    if not isinstance(state, dict) or state.get('schema') != 2 or state.get('strategy') != 'probability_price_volume_v1':
        raise SafetyError('INCOMPATIBLE_DATABASE')
    if not isinstance(state.get('legs'),dict) or set(state['legs']) != {'BTCUSDT','ETHUSDT'}:
        raise SafetyError('INCOMPATIBLE_DATABASE')
    if any(not isinstance(leg,dict) or 'position' not in leg or 'pending' not in leg for leg in state['legs'].values()):
        raise SafetyError('INCOMPATIBLE_DATABASE')
    if occupied(state):
        raise SafetyError('LOCAL_POSITION_OR_PENDING')
    if state.get('halt') not in (COLLATERAL_HALT, None):
        raise SafetyError('DIFFERENT_HALT: no other halt can be cleared here')
    after = copy.deepcopy(state)
    after['halt'] = None
    return after


def recover_collateral(root, api=None):
    from .cli import legacy_state_guard, legacy_process_guard
    root = Path(root).resolve()
    path = root / 'data/live.sqlite'
    if not path.is_file() or path.is_symlink():
        raise SafetyError('LIVE_DATABASE_REQUIRED')
    service_stopped(root)
    with runtime.lock(root,'live'), runtime.lock(root,'paper'):
        if (root / 'data/LIVE_ENABLED.json').exists():
            raise SafetyError('PAUSE_NEW_ENTRIES_BEFORE_RECOVERY')
        legacy_state_guard(root)
        legacy_process_guard(root)
        # Read first without changing journal settings or creating a new DB.
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            row=db.execute("SELECT json FROM state WHERE key='engine'").fetchone()
            uid_row=db.execute("SELECT json FROM state WHERE key='account_uid'").fetchone()
        if not row or not uid_row:
            raise SafetyError('BOUND_ACCOUNT_STATE_REQUIRED')
        before=json.loads(row[0]);bound=json.loads(uid_row[0])
        after=cleared_state(before)
        c=config(root)
        if api is None:
            api=Rest(credentials(runtime.connection(root)['env']),write=False)
        info=api.get('/api/v3/account/info',private=True)
        if not bound or not info.get('userId') or str(bound)!=str(info['userId']):
            raise SafetyError('ACCOUNT_CHANGED')
        for index in range(2):
            snapshot=account.inventory(api)
            account.require_flat(snapshot)
            margin=account.margin_breakdown(snapshot['assets'])
            if (margin['available_usdt']<c['min_account_equity_usdt']
                    or snapshot['equity']<c['min_account_equity_usdt']):
                raise SafetyError('INSUFFICIENT_910_USDT_WITH_RESERVE')
            if index==0:time.sleep(2)
        service_stopped(root)
        legacy_state_guard(root)
        legacy_process_guard(root)
        if (root/'data/LIVE_ENABLED.json').exists():
            raise SafetyError('LIVE_ARM_REAPPEARED')
        if before.get('halt') is None:
            return {'result':'COLLATERAL_ALREADY_CLEAR',**margin,'new_bot_started':False}
        backup_dir=root/'data/collateral_recovery_backups'/str(time.time_ns())
        backup_dir.mkdir(parents=True,mode=0o700)
        audit={'at':now_ms(),'previous_halt':COLLATERAL_HALT,**margin,
               'exchange_writes':0,'risk_baselines_reset':False}
        with sqlite3.connect(path,timeout=10) as db:
            with sqlite3.connect(backup_dir/'live.sqlite') as backup:db.backup(backup)
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            try:
                row=db.execute("SELECT json FROM state WHERE key='engine'").fetchone()
                current_uid=db.execute("SELECT json FROM state WHERE key='account_uid'").fetchone()
                if (not row or json.loads(row[0])!=before or not current_uid
                        or json.loads(current_uid[0])!=bound):
                    raise SafetyError('STATE_CHANGED_DURING_RECOVERY')
                db.execute("UPDATE state SET json=? WHERE key='engine'",
                           (json.dumps(after,allow_nan=False),))
                db.execute('INSERT INTO events(ts,kind,json) VALUES(?,?,?)',
                           (audit['at'],'COLLATERAL_HALT_RECOVERED',json.dumps(audit,allow_nan=False)))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return {'result':'COLLATERAL_RECOVERED',**margin,'backup':str(backup_dir),
                'new_bot_started':False,'exchange_writes':0}
