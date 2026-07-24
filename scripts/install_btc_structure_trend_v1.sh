#!/usr/bin/env bash
set -Eeuo pipefail

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${1:-${INDEX_SNIPER_ROOT:-$HOME/index-sniper-pro}}"
[[ -d "$ROOT" ]] || { echo "프로젝트 경로가 없습니다: $ROOT" >&2; exit 1; }

PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
[[ -x "$PY" ]] || { echo "Python을 찾을 수 없습니다." >&2; exit 1; }

cd "$SRC_DIR"
if [[ -f SHA256SUMS ]] && command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c SHA256SUMS
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/local_backups/btc_structure_trend_v1_$STAMP"
mkdir -p "$BACKUP/index_sniper" "$BACKUP/config" "$BACKUP/scripts"

FILES=(
  "index_sniper/btc_structure_trend_v1.py"
  "config/btc_structure_trend_v1.json"
  "_btc_structure_common.sh"
  "doctor_btc_structure_trend_v1.sh"
  "setup_btc_structure_trend_v1_account.sh"
  "arm_btc_structure_trend_v1.sh"
  "disarm_btc_structure_trend_v1.sh"
  "start_btc_structure_trend_v1.sh"
  "start_btc_structure_trend_v1_observe.sh"
  "start_btc_structure_trend_v1_shadow.sh"
  "status_btc_structure_trend_v1_shadow.sh"
  "reset_btc_structure_trend_v1_shadow.sh"
  "stop_btc_structure_trend_v1.sh"
  "status_btc_structure_trend_v1.sh"
)
for f in "${FILES[@]}"; do
  if [[ -e "$ROOT/$f" ]]; then
    mkdir -p "$BACKUP/$(dirname "$f")"
    cp -a "$ROOT/$f" "$BACKUP/$f"
  fi
done
[[ -f "$ROOT/.env" ]] && cp -a "$ROOT/.env" "$BACKUP/.env"
[[ -f "$ROOT/data/BTC_STRUCTURE_TREND_V1_ARMED.json" ]] && cp -a "$ROOT/data/BTC_STRUCTURE_TREND_V1_ARMED.json" "$BACKUP/"

screen -S btc-structure-trend-live -X quit >/dev/null 2>&1 || true
screen -S btc-structure-trend-observe -X quit >/dev/null 2>&1 || true
screen -S btc-structure-trend-shadow -X quit >/dev/null 2>&1 || true
pkill -f '[i]ndex_sniper[.]btc_structure_trend_v1.*loop' 2>/dev/null || true

mkdir -p "$ROOT/index_sniper" "$ROOT/config" "$ROOT/data" "$ROOT/logs" "$ROOT/research" "$ROOT/local_backups"
cp "$SRC_DIR/index_sniper/btc_structure_trend_v1.py" "$ROOT/index_sniper/btc_structure_trend_v1.py"
cp "$SRC_DIR/config/btc_structure_trend_v1.json" "$ROOT/config/btc_structure_trend_v1.json"
for f in "$SRC_DIR"/scripts/*.sh; do
  base="$(basename "$f")"
  [[ "$base" == "install_btc_structure_trend_v1.sh" ]] && continue
  [[ "$base" == "run_btc_structure_trend_v1_tests.sh" ]] && continue
  cp "$f" "$ROOT/$base"
done
cp "$SRC_DIR/scripts/install_btc_structure_trend_v1.sh" "$ROOT/install_btc_structure_trend_v1.sh"
cp "$SRC_DIR/README_KO.md" "$ROOT/README_BTC_STRUCTURE_TREND_V1.md"

chmod 700 "$ROOT"/_btc_structure_common.sh "$ROOT"/*_btc_structure_trend_v1*.sh
chmod 600 "$ROOT/config/btc_structure_trend_v1.json"
touch "$ROOT/.env"
chmod 600 "$ROOT/.env"

# Always install DISARMED. Existing API keys are preserved.
"$PY" - "$ROOT/.env" <<'PYENV'
from pathlib import Path
import os,sys,tempfile
p=Path(sys.argv[1]); key='BTC_STRUCTURE_V1_LIVE_ENABLED'; value='false'
lines=p.read_text(encoding='utf-8',errors='ignore').splitlines() if p.exists() else []
out=[]; found=False
for line in lines:
    if line.lstrip().startswith('#') or '=' not in line:
        out.append(line); continue
    if line.split('=',1)[0].strip()==key:
        if not found: out.append(f'{key}={value}')
        found=True
    else: out.append(line)
if not found: out.append(f'{key}={value}')
fd,tmp=tempfile.mkstemp(prefix='.env.',dir=str(p.parent))
with os.fdopen(fd,'w',encoding='utf-8') as fp:
    fp.write('\n'.join(out).rstrip('\n')+'\n'); fp.flush(); os.fsync(fp.fileno())
os.chmod(tmp,0o600); os.replace(tmp,p)
PYENV
rm -f "$ROOT/data/BTC_STRUCTURE_TREND_V1_ARMED.json"

cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
"$PY" -m py_compile index_sniper/btc_structure_trend_v1.py
"$PY" -m index_sniper.btc_structure_trend_v1 --config config/btc_structure_trend_v1.json self-test

cat <<EOF

============================================================
✅ BTC Structure Trend v1.2 설치 완료 — DISARMED
============================================================
계약: BTCUSDT / cross(crossed) 5x / 수량기준 증거금환산 50%
명목 포지션: 계좌자산의 약 2.5배
백업: $BACKUP

1) 먼저 쉐도우 모드 — Bitget API 키 없이 공개 시세로 가상매매
   bash start_btc_structure_trend_v1_shadow.sh 10000
   bash status_btc_structure_trend_v1_shadow.sh

   단순 신호만 보려면:
   bash start_btc_structure_trend_v1_observe.sh

2) 실매매를 검토할 때만 인증 API·계정 점검
   bash doctor_btc_structure_trend_v1.sh

3) 전용 계정을 cross + hedge + BTC 5x로 설정
   BTC_STRUCTURE_CONFIRM_CROSS_ACCOUNT_MODE=YES \\
     bash setup_btc_structure_trend_v1_account.sh

4) 쉐도우 결과 검토 후 실매매 ARM
   bash arm_btc_structure_trend_v1.sh \\
     START_BTC_STRUCTURE_TREND_LIVE_5X_CROSS_50 \\
     I_UNDERSTAND_CROSS_2_5X_NOTIONAL_CAN_USE_FULL_COLLATERAL \\
     API_HAS_NO_WITHDRAW_PERMISSION \\
     API_IP_WHITELISTED

5) 실매매 엔진 시작
   bash start_btc_structure_trend_v1.sh
============================================================
EOF
