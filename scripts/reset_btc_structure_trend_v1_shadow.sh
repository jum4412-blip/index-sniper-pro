#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_btc_structure_common.sh"
SEED="${1:-10000}"
[[ "$SEED" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "시드는 양수 숫자여야 합니다." >&2; exit 2; }
stop_session "$SHADOW_SESSION"
"$PY" -m "$MODULE" --config "$CONFIG" shadow-reset RESET_SHADOW --seed "$SEED"
echo "✅ 쉐도우 계정을 ${SEED} USDT로 초기화했습니다. 기존 기록은 .bak으로 보관됩니다."
