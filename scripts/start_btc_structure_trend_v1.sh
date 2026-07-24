#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_btc_structure_common.sh"
stop_session "$OBSERVE_SESSION"
stop_session "$SHADOW_SESSION"
stop_session "$LIVE_SESSION"
pkill -f '[i]ndex_sniper[.]btc_structure_trend_v1.*loop' 2>/dev/null || true
mkdir -p "$ROOT/logs"
screen -dmS "$LIVE_SESSION" bash -lc "cd '$ROOT'; exec env PYTHONPATH='$ROOT' '$PY' -m '$MODULE' --config '$CONFIG' loop >> '$LOG' 2>&1"
sleep 2
screen -list | grep -F "$LIVE_SESSION" >/dev/null || { echo "실행 실패. 로그: $LOG" >&2; exit 1; }
echo "✅ LIVE-GATED 엔진 시작: $LIVE_SESSION"
echo "ARM이 유효하지 않으면 관찰·포지션 관리만 하고 신규 진입은 하지 않습니다."
