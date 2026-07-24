#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_btc_structure_common.sh"
echo "===== screen ====="
screen -list 2>/dev/null | grep -E 'btc-structure-trend-(live|observe|shadow)' || true
echo
echo "===== engine status ====="
"$PY" -m "$MODULE" --config "$CONFIG" status
echo
echo "===== last log ====="
tail -n 30 "$LOG" 2>/dev/null || true
