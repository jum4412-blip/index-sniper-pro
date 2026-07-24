#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_btc_structure_common.sh"
echo "===== shadow screen ====="
screen -list 2>/dev/null | grep -F "$SHADOW_SESSION" || true
echo
echo "===== shadow portfolio ====="
"$PY" -m "$MODULE" --config "$CONFIG" shadow-status
echo
echo "===== last shadow log ====="
tail -n 40 "$SHADOW_LOG" 2>/dev/null || true
