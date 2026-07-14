#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_larry_common.sh"

mkdir -p "$ROOT/logs" "$ROOT/data" "$ROOT/research"
if process_running; then
  echo "이미 Larry Core가 실행 중입니다."
  show_processes
  exit 1
fi

if command -v screen >/dev/null 2>&1; then
  screen -S "$SESSION" -X quit >/dev/null 2>&1 || true
  screen -dmS "$SESSION" bash -lc "cd '$ROOT' && exec '$PY' -m index_sniper.larry_williams_core_v1 --config '$CONFIG' loop >> '$LOGFILE' 2>&1"
else
  nohup "$PY" -m index_sniper.larry_williams_core_v1 --config "$CONFIG" loop >> "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
fi
sleep 2
if ! process_running; then
  echo "❌ Larry Core 시작 실패" >&2
  tail -n 80 "$LOGFILE" 2>/dev/null || true
  exit 1
fi
echo "✅ Larry Core LIVE-GATED 실행"
echo "ARM 전에는 실주문이 차단됩니다."
show_processes
