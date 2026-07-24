#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_btc_structure_common.sh"
SEED="${1:-}"

# Starting shadow mode forcibly closes all engine sessions and disables the
# live-entry gate.  The public-only Python client cannot send authenticated
# requests or POST orders even if real API keys remain in .env.
set_env_key BTC_STRUCTURE_V1_LIVE_ENABLED false
"$PY" -m "$MODULE" --config "$CONFIG" disarm >/dev/null
stop_session "$LIVE_SESSION"
stop_session "$OBSERVE_SESSION"
stop_session "$SHADOW_SESSION"
pkill -f '[i]ndex_sniper[.]btc_structure_trend_v1.*loop' 2>/dev/null || true
mkdir -p "$ROOT/logs" "$ROOT/data" "$ROOT/research"

ARGS=(shadow-loop)
if [[ -n "$SEED" ]]; then
  [[ "$SEED" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "시드는 양수 숫자여야 합니다." >&2; exit 2; }
  ARGS+=(--seed "$SEED")
fi

screen -dmS "$SHADOW_SESSION" bash -lc "cd '$ROOT'; exec env PYTHONPATH='$ROOT' '$PY' -m '$MODULE' --config '$CONFIG' ${ARGS[*]} >> '$SHADOW_LOG' 2>&1"
sleep 2
screen -list | grep -F "$SHADOW_SESSION" >/dev/null || { echo "SHADOW 실행 실패. 로그: $SHADOW_LOG" >&2; exit 1; }
echo "✅ SHADOW 시작: $SHADOW_SESSION"
echo "실주문·계정조회 없음. Bitget 공개 시세로 가상 체결과 손익만 기록합니다."
[[ -n "$SEED" ]] && echo "초기 시드 요청: $SEED USDT (기존 쉐도우 상태가 있으면 기존 시드 유지)"
echo "상태: bash status_btc_structure_trend_v1_shadow.sh"
