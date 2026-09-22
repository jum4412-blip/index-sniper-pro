#!/usr/bin/env python3
"""One-time handoff for already-paused predecessors; not a trading patch.

Run from the installed ETH root: python3 -B /path/to/this_file.py
No trade/cancel/leverage API calls, no old/new DB writes, no code/hash changes.
Refuses to pause any unidentified CLI: all old authorization files MUST already
be absent. Reuses original strict account inventory and UID-bound snapshots.
Only known, matching system services and exact observed manager identities stop.
"""
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import time

EXPECTED_ETH_FINGERPRINT = '4ac1a98d169e2019389178af97ddea4f5cfd62710be62e1770682412ec62c4f4'

UNITS = (
    'btc-eth-hype-reversal.service',
    'btc-eth-larry-trend-v2.service',
    'btc-eth-psych-trend.service',
    'btc-eth-trend.service',
    'btc-eth-larry-basic.service',
    'larry-v2-live.service',
)


def main():
    root = Path.cwd().resolve()
    if not (root / 'eth_core/migration.py').is_file():
        raise RuntimeError('먼저 ~/eth_larry_focus_500 폴더로 이동하세요.')
    sys.path.insert(0, str(root))
    from eth_core import account, migration
    from eth_core.cli import api_for, verification, read_database
    from eth_core.runtime import running
    from eth_core.core import SafetyError

    # No persistent code or manifest patch is needed for this handoff.
    original = verification(root)['fingerprint']
    if original != EXPECTED_ETH_FINGERPRINT:
        raise SafetyError('ETH_RELEASE_DIFFERS: 소스가 다르므로 종료하지 않습니다.')
    if running(root, 'live'):
        raise SafetyError('NEW_ETH_MANAGER_RUNNING: 종료 절차를 겹쳐 실행하지 마세요.')
    if (root / 'data/LIVE_ENABLED.json').exists():
        raise SafetyError('NEW_ETH_ENTRIES_ENABLED: 먼저 ./eth pause 실행')
    new_state = read_database(root, 'live').get('engine')
    if new_state is not None and migration.occupied(new_state):
        raise SafetyError('NEW_ETH_POSITION_OR_PENDING: 관리자를 먼저 점검하세요.')

    specs = migration.sources(root)
    known = {str(s['root']) for s in specs}
    found = migration.managers(root)
    if any(p['root'] not in known for p in found):
        raise SafetyError('UNKNOWN_MANAGER_ROOT: 자동 종료하지 않습니다.')
    if any(p['module'] not in ('basic_core', 'trend_core', 'larry_live') for p in found):
        raise SafetyError('UNSUPPORTED_MANAGER: 자동 종료하지 않습니다.')

    api = api_for(root, write=False)

    def check():
        # Compare source discovery again, not just the first file snapshot.
        if migration.sources(root) != specs:
            raise SafetyError('PREDECESSOR_SET_CHANGED')
        for spec in specs:
            if any(p.exists() for p in spec['arm']):
                raise SafetyError('OLD_ENTRY_AUTH_PRESENT: ' + str(spec['root']))
            snap = migration.snapshot(spec)
            migration.bound(api, snap['uid'])
            if migration.occupied(snap['state']):
                raise SafetyError('OLD_POSITION_OR_PENDING: ' + str(spec['root']))
        inventory = account.inventory(api)
        account.require_flat(inventory)
        if any(p not in found for p in migration.managers(root)):
            raise SafetyError('MANAGER_IDENTITY_CHANGED: 종료하지 않습니다.')
        return inventory

    for i in range(2):
        snap = check()
        print(json.dumps({
            'flat_check': i + 1,
            'positions': len(snap['positions']),
            'orders': len(snap['orders']),
            'strategies': len(snap['strategies']),
            'same_account_verified': True,
        }, ensure_ascii=False), flush=True)
        if i == 0:
            time.sleep(2)

    for unit in UNITS:
        result = subprocess.run(
            ['systemctl', 'show', unit,
             '--property=LoadState,WorkingDirectory,ExecStart,ExecStop,ExecStopPost'],
            text=True, capture_output=True, timeout=20,
        )
        info = dict(x.split('=', 1) for x in result.stdout.splitlines() if '=' in x)
        if info.get('LoadState') == 'not-found':
            continue
        if result.returncode != 0 or not info.get('LoadState'):
            raise SafetyError('SERVICE_QUERY_FAILED: ' + unit)
        if info.get('WorkingDirectory') not in known:
            continue
        command = info.get('ExecStart', '')
        if not any(x in command for x in ('-m basic_core live', '-m trend_core live', '-m larry_live')):
            raise SafetyError('UNRECOGNIZED_OLD_SERVICE_EXEC: ' + unit)
        if info.get('ExecStop') or info.get('ExecStopPost'):
            raise SafetyError('CUSTOM_SERVICE_STOP_HOOK: ' + unit)
        check()
        print('이전 서비스 종료/자동시작 해제:', unit, flush=True)
        subprocess.run(
            ['sudo', '-n', 'systemctl', 'disable', '--now', unit],
            check=True, timeout=140,
        )

    # Services are stopped first so systemd cannot respawn their managers.
    for proc in migration.managers(root):
        check()
        if proc not in found:
            raise SafetyError('MANAGER_CHANGED_DURING_HANDOFF')
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise SafetyError('PIDFD_UNAVAILABLE: PID를 임의로 종료하지 않습니다.')
        try:
            fd = os.pidfd_open(proc['pid'])
        except ProcessLookupError:
            continue
        try:
            if proc not in migration.managers(root):
                raise SafetyError('PID_IDENTITY_CHANGED')
            print('확인된 이전 관리자 정상 종료 요청:', proc['pid'], flush=True)
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)

    deadline = time.monotonic() + 30
    while migration.managers(root) and time.monotonic() < deadline:
        time.sleep(1)
    migration.ensure_previous_safe(root)
    check()
    if verification(root)['fingerprint'] != original:
        raise SafetyError('ETH_CODE_CHANGED_DURING_HANDOFF')
    print('PREVIOUS_RETIRED: 이전 관리자 없음 / 거래소 FLAT 확인', flush=True)
    print('ETH 코드·거래 DB·손실기록 보존. 새 ETH 봇은 아직 시작하지 않았습니다.', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('취소됨. 재시작·재승인·주문·자동청산은 수행하지 않습니다.', file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        # No raw account payloads, authentication material, or process argv.
        core = sys.modules.get('eth_core.core')
        safety = getattr(core, 'SafetyError', RuntimeError)
        message = str(exc) if isinstance(exc, (safety, RuntimeError)) else type(exc).__name__
        print('STOP:', message, file=sys.stderr)
        raise SystemExit(2)
