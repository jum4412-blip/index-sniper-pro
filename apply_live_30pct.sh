#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo "🛑 .env 파일이 없습니다. 먼저 .env를 만들어주세요."
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
    'MAX_ORDER_NOTIONAL_USDT':'1000',
    'MAX_LIVE_CAPITAL_RATIO':'0.30',
    'MAX_DAILY_LOSS_PCT':'1.00',
    'INDEX_WEEKEND_FLAT':'true',
    'INDEX_WEEKEND_AUTO_CLOSE':'true',
    'INDEX_WEEKEND_FLAT_SYMBOLS':'SP500USDT,NDX100USDT',
    'INDEX_WEEKEND_BLOCK_NEW_AFTER_ET':'15:30',
    'INDEX_WEEKEND_FORCE_FLAT_AFTER_ET':'16:30',
    'INDEX_WEEKEND_REOPEN_AFTER_ET':'18:30',
}
seen=set()
out=[]
for line in lines:
    if '=' in line and not line.lstrip().startswith('#'):
        k=line.split('=',1)[0].strip()
        if k in updates:
            out.append(f'{k}={updates[k]}')
            seen.add(k)
            continue
    out.append(line)
if out and out[-1].strip():
    out.append('')
for k,v in updates.items():
    if k not in seen:
        out.append(f'{k}={v}')
p.write_text('\n'.join(out)+'\n', encoding='utf-8')
PY

echo "✅ .env updated for LIVE 10% survival mode"
echo "백업: $backup"
echo "---- 확인 ----"
grep -E '^(DRY_RUN|LIVE_TRADING_ENABLED|CAPITAL_RATIO|MAX_LIVE_CAPITAL_RATIO|MAX_ORDER_NOTIONAL_USDT|MAX_DAILY_LOSS_PCT|INDEX_WEEKEND_)=' .env || true
echo "⚠️ 기존 포지션이 있으면 먼저 수동 정리하고 run_live_preflight.sh를 실행하세요."
