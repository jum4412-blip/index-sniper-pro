#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_larry_common.sh"

mkdir -p "$ROOT/logs" "$ROOT/data" "$ROOT/research"
if process_running; then
  echo "이미 실행 중입니다. 먼저 stop_larry_core_v1.sh를 실행하세요."
  exit 1
fi
if command -v screen >/dev/null 2>&1; then
  screen -dmS "$SESSION" bash -lc "cd '$ROOT' && exec '$PY' -m index_sniper.larry_williams_core_v1 --config '$CONFIG' loop --observe >> '$LOGFILE' 2>&1"
else
  nohup "$PY" -m index_sniper.larry_williams_core_v1 --config "$CONFIG" loop --observe >> "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
fi
echo "✅ 관찰 모드 실행"
