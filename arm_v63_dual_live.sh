#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
LIVE="${1:-}"
ROTATE="${2:-}"
NOWITHDRAW="${3:-}"
IPWHITE="${4:-}"
if [[ "$LIVE" != "START_V63_DUAL_LIVE_5X_BTC_ETH" || \
      "$ROTATE" != "I_ROTATED_ALL_EXPOSED_KEYS" || \
      "$NOWITHDRAW" != "API_HAS_NO_WITHDRAW_PERMISSION" || \
      "$IPWHITE" != "API_IP_WHITELISTED" ]]; then
  cat <<'EOF'
❌ 확인 문구가 정확하지 않습니다.

bash arm_v63_dual_live.sh \
  START_V63_DUAL_LIVE_5X_BTC_ETH \
  I_ROTATED_ALL_EXPOSED_KEYS \
  API_HAS_NO_WITHDRAW_PERMISSION \
  API_IP_WHITELISTED
EOF
  exit 2
fi
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
export PYTHONPATH="$ROOT"
mkdir -p "$ROOT/data/v63_dual_live"
# Prevent a race while pre-arm checks run.
printf 'arming_check=%s\n' "$(date -Is)" > "$ROOT/data/v63_dual_live/PAUSE_NEW_ENTRIES"
bash "$ROOT/stop_v63_dual_live.sh" >/dev/null 2>&1 || true
rm -f "$ROOT/data/v63_dual_live/LIVE_ARMED"
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
    if m and m.group(1) in keys:
        continue
    out.append(line)
if out and out[-1].strip(): out.append('')
for k,v in values.items(): out.append(f'{k}="{v}"')
p.write_text('\n'.join(out)+'\n',encoding='utf-8')
p.chmod(0o600)
PY

echo "🔎 pre-arm doctor 실행..."
if ! "$PY" -m index_sniper.dual_live_v63 doctor --prearm; then
  echo "❌ pre-arm 검사 실패. LIVE는 비활성 상태로 유지됩니다."
  if command -v systemctl >/dev/null 2>&1 && systemctl cat index-sniper-v63.service >/dev/null 2>&1; then
    sudo systemctl start index-sniper-v63.service || true
  fi
  exit 1
fi

ROOT_ENV="$ROOT" LIVE_VALUE="$LIVE" ROTATE_VALUE="$ROTATE" NOWITHDRAW_VALUE="$NOWITHDRAW" IPWHITE_VALUE="$IPWHITE" "$PY" - <<'PY'
from pathlib import Path
import os,re
root=Path(os.environ['ROOT_ENV'])
p=root/'.env'
lines=p.read_text(encoding='utf-8',errors='ignore').splitlines() if p.exists() else []
values={
 'V63_CONFIG':'config/v63_dual_live.json',
 'V63_NOTIFY':'true',
 'V63_FORCE_SHADOW':'false',
 'V63_LIVE_ENABLED':'true',
 'V63_LIVE_CONFIRM':os.environ['LIVE_VALUE'],
 'V63_KEYS_ROTATED_CONFIRM':os.environ['ROTATE_VALUE'],
 'V63_NO_WITHDRAW_CONFIRM':os.environ['NOWITHDRAW_VALUE'],
 'V63_IP_WHITELIST_CONFIRM':os.environ['IPWHITE_VALUE'],
}
keys=set(values)
out=[]
for line in lines:
    m=re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=',line.strip())
    if m and m.group(1) in keys:
        continue
    out.append(line)
if out and out[-1].strip(): out.append('')
for k,v in values.items(): out.append(f'{k}="{v}"')
p.write_text('\n'.join(out)+'\n',encoding='utf-8')
p.chmod(0o600)
arm=root/'data/v63_dual_live/LIVE_ARMED'
arm.parent.mkdir(parents=True,exist_ok=True)
arm.write_text(os.environ['LIVE_VALUE']+'\n',encoding='utf-8')
arm.chmod(0o600)
PY
rm -f "$ROOT/data/v63_dual_live/PAUSE_NEW_ENTRIES"
if command -v systemctl >/dev/null 2>&1 && systemctl cat index-sniper-v63.service >/dev/null 2>&1; then
  sudo systemctl restart index-sniper-v63.service
else
  bash "$ROOT/start_v63_dual_live.sh"
fi
sleep 3
echo "✅ v6.3 LIVE 활성화 완료"
bash "$ROOT/status_v63_dual_live.sh"
