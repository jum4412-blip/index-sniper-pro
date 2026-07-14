#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_larry_common.sh"

screen -S "$SESSION" -X quit >/dev/null 2>&1 || true
if [[ -f "$PIDFILE" ]]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]]; then
    kill "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$PIDFILE"
fi
pkill -f "$MODULE_PATTERN" >/dev/null 2>&1 || true
sleep 1
if process_running; then
  echo "❌ 프로세스가 남아 있습니다." >&2
  show_processes
  exit 1
fi
echo "✅ Larry Core 프로세스 중지"
echo "주의: 이 명령은 포지션 청산 명령이 아닙니다."
