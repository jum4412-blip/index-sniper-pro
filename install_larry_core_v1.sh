#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="${1:-${INDEX_SNIPER_ROOT:-$HOME/index-sniper-pro}}"

if [[ ! -d "$ROOT" ]]; then
  echo "프로젝트 경로가 없습니다: $ROOT" >&2
  exit 1
fi
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP="$ROOT/local_backups/larry_core_v1_$STAMP"
mkdir -p "$BACKUP/index_sniper" "$BACKUP/config" "$BACKUP/scripts"

FILES=(
  "index_sniper/larry_williams_core_v1.py"
  "config/larry_williams_core_v1.json"
  "_larry_common.sh"
  "doctor_larry_core_v1.sh"
  "setup_larry_core_v1_account.sh"
  "arm_larry_core_v1.sh"
  "disarm_larry_core_v1.sh"
  "start_larry_core_v1.sh"
  "start_larry_core_v1_observe.sh"
  "stop_larry_core_v1.sh"
  "status_larry_core_v1.sh"
)

for f in "${FILES[@]}"; do
  if [[ -e "$ROOT/$f" ]]; then
    mkdir -p "$BACKUP/$(dirname "$f")"
    cp -a "$ROOT/$f" "$BACKUP/$f"
  fi
done

# Stop only known legacy live executors. This never sends a close-position order.
bash stop_v63_dual_live.sh 2>/dev/null || true
pkill -f '[i]ndex_sniper.dual_live_v63' 2>/dev/null || true
pkill -f '[i]ndex_sniper.larry_williams_core_v1 loop' 2>/dev/null || true

mkdir -p index_sniper config data logs research local_backups
cp "$SRC_DIR/index_sniper/larry_williams_core_v1.py" index_sniper/larry_williams_core_v1.py
cp "$SRC_DIR/config/larry_williams_core_v1.json" config/larry_williams_core_v1.json
for f in "$SRC_DIR"/scripts/*.sh; do
  cp "$f" "$(basename "$f")"
done
chmod 700 _larry_common.sh *_larry_core_v1.sh
chmod 600 config/larry_williams_core_v1.json

# Install disarmed. Explicit arm phrases are required later.
touch .env
TMP="$(mktemp)"
grep -v -E '^[[:space:]]*LARRY_V1_LIVE_ENABLED[[:space:]]*=' .env > "$TMP" || true
printf '\nLARRY_V1_LIVE_ENABLED="false"\n' >> "$TMP"
mv "$TMP" .env
chmod 600 .env

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
"$PY" -m py_compile index_sniper/larry_williams_core_v1.py
"$PY" -m index_sniper.larry_williams_core_v1 --config config/larry_williams_core_v1.json self-test

echo
echo "✅ 설치 완료"
echo "백업: $BACKUP"
echo "현재 상태: DISARMED — 실주문 없음"
echo
echo "1) 점검"
echo "   bash doctor_larry_core_v1.sh"
echo "2) ETHUSDT + SKHYUSDT Cross 5x 설정"
echo "   bash setup_larry_core_v1_account.sh"
echo "3) 엔진 실행(아직 신규 진입 차단 상태)"
echo "   bash start_larry_core_v1.sh"
echo "4) 실매매 활성화"
cat <<'EOF'
   bash arm_larry_core_v1.sh \
     START_LARRY_CORE_LIVE_5X_CROSS_30_ETH_SKHY \
     I_UNDERSTAND_30PCT_MARGIN_5X \
     API_HAS_NO_WITHDRAW_PERMISSION \
     API_IP_WHITELISTED
EOF
