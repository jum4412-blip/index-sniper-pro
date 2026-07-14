#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_larry_common.sh"

if [[ "$#" -ne 4 ]]; then
  cat <<'EOF'
사용법:
  bash arm_larry_core_v1.sh \
    START_LARRY_CORE_LIVE_5X_CROSS_30_ETH_SKHY \
    I_UNDERSTAND_30PCT_MARGIN_5X \
    API_HAS_NO_WITHDRAW_PERMISSION \
    API_IP_WHITELISTED
EOF
  exit 2
fi

"$PY" -m index_sniper.larry_williams_core_v1 --config "$CONFIG" doctor --prearm
"$PY" -m index_sniper.larry_williams_core_v1 --config "$CONFIG" arm "$@"
set_env_key "LARRY_V1_LIVE_ENABLED" "true"
echo "✅ 실매매 진입 게이트 활성화"
echo "엔진이 이미 실행 중이면 다음 주기부터 실진입 가능합니다."
