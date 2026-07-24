#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_btc_structure_common.sh"
stop_session "$LIVE_SESSION"
stop_session "$OBSERVE_SESSION"
stop_session "$SHADOW_SESSION"
pkill -f '[i]ndex_sniper[.]btc_structure_trend_v1.*loop' 2>/dev/null || true
echo "✅ LIVE / OBSERVE / SHADOW 엔진 정지"
echo "주의: 정지는 청산 명령이 아닙니다. 거래소 하드스톱은 남지만 소프트 구조손절·추적손절은 멈춥니다."
