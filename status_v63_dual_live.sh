#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
export PYTHONPATH="$ROOT"
echo "===== V6.3 PROCESS ====="
if command -v systemctl >/dev/null 2>&1 && systemctl cat index-sniper-v63.service >/dev/null 2>&1; then
  systemctl is-active index-sniper-v63.service || true
else
  echo "systemd unit not installed"
fi
pgrep -af 'python(3)? .*index_sniper[.]dual_live_v63 loop' || echo "no v6.3 loop"
echo
echo "===== ARM / PAUSE ====="
ROOT_ENV="$ROOT" "$PY" - <<'PY'
from pathlib import Path
import os
try:
    from dotenv import dotenv_values
    values = dotenv_values(Path(os.environ['ROOT_ENV'])/'.env')
except Exception:
    values = {}
keys = [
    'V63_LIVE_ENABLED','V63_FORCE_SHADOW','V63_LIVE_CONFIRM','V63_KEYS_ROTATED_CONFIRM',
    'V63_NO_WITHDRAW_CONFIRM','V63_IP_WHITELIST_CONFIRM'
]
for key in keys:
    value = str(values.get(key) or '')
    if key in {'V63_LIVE_ENABLED','V63_FORCE_SHADOW'}:
        print(f"{key}={value or 'false'}")
    else:
        print(f"{key}={'SET' if value else 'MISSING'}")
root=Path(os.environ['ROOT_ENV'])
print('LIVE_ARMED_FILE=' + ('YES' if (root/'data/v63_dual_live/LIVE_ARMED').exists() else 'NO'))
print('PAUSE_NEW_ENTRIES=' + ('YES' if (root/'data/v63_dual_live/PAUSE_NEW_ENTRIES').exists() else 'NO'))
PY

echo
echo "===== LAST SNAPSHOT ====="
ROOT_ENV="$ROOT" "$PY" - <<'PY'
from pathlib import Path
import json, os
p=Path(os.environ['ROOT_ENV'])/'data/v63_dual_live/state.json'
if not p.exists():
    print('no state.json yet')
    raise SystemExit
try:
    data=json.loads(p.read_text(encoding='utf-8'))
    snap=data.get('last_snapshot') or {}
    print(json.dumps({
        'ts': snap.get('ts'),
        'mode': snap.get('mode'),
        'equity': snap.get('equity'),
        'available': snap.get('available'),
        'guards': snap.get('guards'),
        'open_positions': snap.get('open_positions'),
        'unknown_positions': snap.get('unknown_positions'),
        'external_position_count': snap.get('external_position_count'),
        'external_order_count': snap.get('external_order_count'),
        'uncertain_orders': snap.get('uncertain_orders'),
        'reconciliation_holds': snap.get('reconciliation_holds'),
        'candidates': snap.get('candidates'),
        'selected': snap.get('selected'),
        'global_blockers': snap.get('global_blockers'),
        'errors': snap.get('errors'),
        'signals': {
            k: {
                'price': v.get('price'), 'side': v.get('side'), 'mode': v.get('mode'),
                'regime_long': v.get('regime_long'), 'regime_short': v.get('regime_short'),
                'trigger_long': v.get('trigger_long'), 'trigger_short': v.get('trigger_short'),
                'edge': v.get('edge'), 'volume_z5': v.get('volume_z5'),
                'oi_15m_pct': v.get('oi_15m_pct'), 'event': v.get('event'),
                'blockers': v.get('blockers')
            } for k,v in (snap.get('signals') or {}).items()
        }
    }, ensure_ascii=False, indent=2))
except Exception as exc:
    print(f'state parse error: {exc}')
PY

echo
echo "===== SECURITY AUDIT ====="
"$PY" - <<'PY'
import json
from index_sniper.dual_live_v63 import security_audit
print(json.dumps(security_audit(), ensure_ascii=False, indent=2))
PY

echo
echo "===== LOG TAIL ====="
tail -n 30 "$ROOT/logs/v63-dual-live.log" 2>/dev/null || echo "no log yet"
