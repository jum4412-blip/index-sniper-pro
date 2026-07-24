#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_btc_structure_common.sh"
if [[ "$#" -ne 4 ]]; then
  cat <<'EOF'
사용법:
  bash arm_btc_structure_trend_v1.sh \
    START_BTC_STRUCTURE_TREND_LIVE_5X_CROSS_50 \
    I_UNDERSTAND_CROSS_2_5X_NOTIONAL_CAN_USE_FULL_COLLATERAL \
    API_HAS_NO_WITHDRAW_PERMISSION \
    API_IP_WHITELISTED
EOF
  exit 2
fi
"$PY" -m "$MODULE" --config "$CONFIG" doctor --prearm
"$PY" -m "$MODULE" --config "$CONFIG" arm "$@"
set_env_key BTC_STRUCTURE_V1_LIVE_ENABLED true
echo "✅ 신규 실진입 게이트 활성화"
echo "열린 포지션 관리는 ARM 여부와 무관하게 계속됩니다."
