#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_larry_common.sh"

"$PY" -m index_sniper.larry_williams_core_v1 --config "$CONFIG" disarm
set_env_key "LARRY_V1_LIVE_ENABLED" "false"
echo "✅ 신규 실진입 차단(DISARMED)"
echo "열린 Larry 포지션이 있으면 실행 중인 엔진은 관리를 계속합니다."
