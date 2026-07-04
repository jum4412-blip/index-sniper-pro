#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo "🛑 .env 파일이 없습니다."
  exit 1
fi
backup=".env.bak.$(date -u +%Y%m%d_%H%M%S)"
cp .env "$backup"
python3 - <<'PY'
from pathlib import Path
p=Path('.env')
lines=p.read_text(encoding='utf-8').splitlines()
updates={
    'CAPITAL_RATIO':'0.30',
    'MAX_LIVE_CAPITAL_RATIO':'0.30',
    'MAX_ORDER_NOTIONAL_USDT':'1000',
    'MAX_DAILY_LOSS_PCT':'1.00',
    'ANTI_CHASE_ENABLED':'true',
    'ANTI_CHASE_SYMBOLS':'SP500USDT,NDX100USDT,BTCUSDT',
    'ANTI_CHASE_EXTREME_UP_PCT':'7.0',
    'ANTI_CHASE_EXTREME_DOWN_PCT':'7.0',
    'ANTI_CHASE_EXTREME_RANGE_ATR':'1.8',
    'ANTI_CHASE_EXTREME_LONG_SIZE_MULTIPLIER':'0.0',
    'ANTI_CHASE_EXTREME_SHORT_SIZE_MULTIPLIER':'0.0',
    'MAX_ENTRY_EXTENSION_ATR':'0.40',
    'POSITION_MANAGER_ENABLED':'true',
    'POSITION_WARN_AFTER_HOURS':'24',
    'POSITION_MAX_HOLD_HOURS_INDEX':'48',
    'POSITION_MAX_HOLD_HOURS_BTC':'72',
    'POSITION_BREAKEVEN_ALERT_R':'1.0',
    'POSITION_MANAGER_AUTO_CLOSE':'false',
}
seen=set(); out=[]
for line in lines:
    if '=' in line and not line.lstrip().startswith('#'):
        key=line.split('=',1)[0].strip()
        if key in updates:
            out.append(f'{key}={updates[key]}')
            seen.add(key)
            continue
    out.append(line)
if out and out[-1].strip(): out.append('')
for k,v in updates.items():
    if k not in seen: out.append(f'{k}={v}')
p.write_text('\n'.join(out)+'\n', encoding='utf-8')
PY

echo "✅ .env updated for v2.0 survival momentum 30pct"
echo "백업: $backup"
grep -E '^(CAPITAL_RATIO|MAX_ORDER_NOTIONAL_USDT|MAX_DAILY_LOSS_PCT|ANTI_CHASE|MAX_ENTRY_EXTENSION_ATR|POSITION_)=' .env || true
