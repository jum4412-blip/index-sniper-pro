#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_btc_structure_common.sh"
set_env_key BTC_STRUCTURE_V1_LIVE_ENABLED false
"$PY" -m "$MODULE" --config "$CONFIG" disarm >/dev/null
stop_session "$LIVE_SESSION"
stop_session "$OBSERVE_SESSION"
stop_session "$SHADOW_SESSION"
pkill -f '[i]ndex_sniper[.]btc_structure_trend_v1.*loop' 2>/dev/null || true
mkdir -p "$ROOT/logs"
screen -dmS "$OBSERVE_SESSION" bash -lc "cd '$ROOT'; exec env PYTHONPATH='$ROOT' '$PY' -m '$MODULE' --config '$CONFIG' loop --observe >> '$LOG' 2>&1"
sleep 2
screen -list | grep -F "$OBSERVE_SESSION" >/dev/null || { echo "실행 실패. 로그: $LOG" >&2; exit 1; }
echo "✅ OBSERVE 시작: $OBSERVE_SESSION — 공개 시세 전용, 주문·계정조회 없음"
