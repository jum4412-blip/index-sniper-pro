#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_btc_structure_common.sh"
cat <<'EOF'
BTC 전용 UTA 계정을 cross margin + hedge mode + BTCUSDT 5x로 설정합니다.
Bitget API의 cross 값은 "crossed"입니다.
포지션, 일반 주문, TP/SL 주문이 하나라도 있으면 중단됩니다.
accountLevel이 이미 basic 또는 advanced면 계정 레벨은 바꾸지 않습니다.
isolated/delta/기타 레벨이면 기본적으로 basic으로 전환합니다.
전환을 허용하려면 다음처럼 실행하세요.
  BTC_STRUCTURE_CONFIRM_CROSS_ACCOUNT_MODE=YES bash setup_btc_structure_trend_v1_account.sh
advanced로 전환하려면 BTC_STRUCTURE_CROSS_ACCOUNT_LEVEL=advanced를 함께 지정하세요.
EOF
set_env_key BTC_STRUCTURE_V1_LIVE_ENABLED false
"$PY" -m "$MODULE" --config "$CONFIG" disarm
"$PY" -m "$MODULE" --config "$CONFIG" setup-account
"$PY" -m "$MODULE" --config "$CONFIG" doctor
