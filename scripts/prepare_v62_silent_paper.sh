#!/usr/bin/env bash
set -euo pipefail

ROOT="${INDEX_SNIPER_ROOT:-$HOME/index-sniper-pro}"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
cd "$ROOT"

if [[ ! -f index_sniper/unified_v62.py ]]; then
  echo "❌ index_sniper/unified_v62.py가 없습니다." >&2
  exit 1
fi

# v6.2는 코드 자체가 paper-only이며 주문 함수가 없다. 여기서는 Telegram만 완전히 끄고
# paper episode/trade 기록은 계속 남도록 한다.
ROOT_ENV="$ROOT" "$PY" - <<'PY'
from pathlib import Path
import os, re
root = Path(os.environ["ROOT_ENV"])
p = root / ".env"
values = {
    "V62_PAPER_ENABLED": "true",
    "V62_NOTIFY": "false",
    "V62_NOTIFY_HEARTBEAT": "false",
}
lines = p.read_text(encoding="utf-8", errors="ignore").splitlines() if p.exists() else []
keys = set(values)
out = []
for line in lines:
    m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    if not (m and m.group(1) in keys):
        out.append(line)
if out and out[-1].strip():
    out.append("")
out.append("# BTC Quant v6.2 — silent paper recorder")
for k, v in values.items():
    out.append(f'{k}="{v}"')
p.write_text("\n".join(out) + "\n", encoding="utf-8")
p.chmod(0o600)
print("v6.2 env: paper=true, notify=false, heartbeat=false")
PY

# 현재 screen/systemd 방식에 관계없이 알려진 스크립트를 우선 사용한다.
if [[ -x ./stop_v62_unified.sh ]]; then
  bash ./stop_v62_unified.sh || true
else
  screen -S btc-v62-unified -X quit >/dev/null 2>&1 || true
  pkill -f '[i]ndex_sniper[.]unified_v62 loop' >/dev/null 2>&1 || true
fi

# v6.2 관련 활성 systemd 서비스가 있으면 같은 서비스를 재시작한다.
mapfile -t V62_SERVICES < <(systemctl list-units --type=service --state=active --no-legend 2>/dev/null \
  | awk '{print $1}' | grep -Ei 'v62|unified.*62|62.*unified' || true)
if (( ${#V62_SERVICES[@]} > 0 )); then
  for svc in "${V62_SERVICES[@]}"; do
    sudo systemctl restart "$svc"
    echo "재시작: $svc"
  done
else
  if [[ -x ./start_v62_unified.sh ]]; then
    bash ./start_v62_unified.sh
  else
    mkdir -p logs
    screen -dmS btc-v62-unified bash -lc "cd '$ROOT' && exec '$PY' -m index_sniper.unified_v62 loop >> logs/v62-unified.log 2>&1"
  fi
fi
sleep 2

echo "===== V6.2 SILENT PAPER STATUS ====="
pgrep -af '[i]ndex_sniper[.]unified_v62 loop' || true
if [[ -x ./status_v62_unified.sh ]]; then
  bash ./status_v62_unified.sh || true
fi

echo "✅ v6.2는 실주문 없이 paper 기록만 계속하고 Telegram 알림은 중지했습니다."
