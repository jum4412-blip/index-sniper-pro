#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
export PYTHONPATH="$ROOT"
mkdir -p "$ROOT/data/v63_dual_live"
printf 'disarm_check=%s\n' "$(date -Is)" > "$ROOT/data/v63_dual_live/PAUSE_NEW_ENTRIES"
bash "$ROOT/stop_v63_dual_live.sh" >/dev/null 2>&1 || true
set +e
"$PY" - <<'PY'
from index_sniper.dual_live_v63 import load_dotenv_safely, DEFAULT_ROOT, make_client, all_current_positions
load_dotenv_safely(DEFAULT_ROOT/'.env')
rows=all_current_positions(make_client())
if rows:
    print(f"OPEN_POSITIONS={len(rows)}")
    for row in rows:
        print(f"- {row.get('symbol')} {row.get('posSide') or row.get('side')} qty={row.get('total') or row.get('size') or row.get('qty')}")
    raise SystemExit(3)
print('OPEN_POSITIONS=0')
PY
RC=$?
set -e
if [[ "$RC" -ne 0 ]]; then
  echo "❌ 열린 USDT 선물 포지션이 있어 disarm을 거부합니다."
  echo "신규 진입은 PAUSE 상태이며, 기존 포지션 관리를 위해 엔진을 다시 시작합니다."
  bash "$ROOT/start_v63_dual_live.sh" || true
  exit "$RC"
fi
ROOT_ENV="$ROOT" "$PY" - <<'PY'
from pathlib import Path
import os,re
root=Path(os.environ['ROOT_ENV'])
p=root/'.env'
lines=p.read_text(encoding='utf-8',errors='ignore').splitlines() if p.exists() else []
values={
 'V63_CONFIG':'config/v63_dual_live.json',
 'V63_NOTIFY':'true',
 'V63_FORCE_SHADOW':'false',
 'V63_LIVE_ENABLED':'false',
 'V63_LIVE_CONFIRM':'',
 'V63_KEYS_ROTATED_CONFIRM':'',
 'V63_NO_WITHDRAW_CONFIRM':'',
 'V63_IP_WHITELIST_CONFIRM':'',
}
keys=set(values)
out=[]
for line in lines:
    m=re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=',line.strip())
    if m and m.group(1) in keys: continue
    out.append(line)
if out and out[-1].strip(): out.append('')
for k,v in values.items(): out.append(f'{k}="{v}"')
p.write_text('\n'.join(out)+'\n',encoding='utf-8')
p.chmod(0o600)
(root/'data/v63_dual_live/LIVE_ARMED').unlink(missing_ok=True)
PY
rm -f "$ROOT/data/v63_dual_live/PAUSE_NEW_ENTRIES"
bash "$ROOT/start_v63_dual_live.sh"
echo "✅ v6.3 SHADOW로 전환 완료"
