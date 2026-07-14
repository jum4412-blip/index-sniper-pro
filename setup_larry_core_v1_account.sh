#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_larry_common.sh"

echo "ETHUSDT와 SKHYUSDT를 Cross 5x로 설정합니다."
echo "기존 포지션/미체결 주문이 있으면 자동 중단됩니다."
"$PY" -m index_sniper.larry_williams_core_v1 --config "$CONFIG" setup-account
"$PY" -m index_sniper.larry_williams_core_v1 --config "$CONFIG" doctor
