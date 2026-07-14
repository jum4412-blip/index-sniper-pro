#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_larry_common.sh"

echo "===== LARRY PROCESS ====="
if process_running; then
  echo "RUNNING"
  show_processes
else
  echo "STOPPED"
fi

echo
echo "===== ARM / STATE ====="
"$PY" -m index_sniper.larry_williams_core_v1 --config "$CONFIG" status || true

echo
echo "===== LOG TAIL ====="
tail -n 40 "$LOGFILE" 2>/dev/null || echo "로그 없음"
