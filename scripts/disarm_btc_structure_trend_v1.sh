#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_btc_structure_common.sh"
set_env_key BTC_STRUCTURE_V1_LIVE_ENABLED false
"$PY" -m "$MODULE" --config "$CONFIG" disarm
echo "✅ 신규 진입 차단. 실행 중인 엔진은 기존 포지션 관리를 계속합니다."
