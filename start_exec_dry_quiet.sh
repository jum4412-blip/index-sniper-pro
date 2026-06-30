#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs
if command -v screen >/dev/null 2>&1; then
  for s in $(screen -ls | awk '/sniper-exec-dry/ {print $1}'); do
    screen -S "$s" -X quit 2>/dev/null || true
  done
  screen -wipe 2>/dev/null || true
fi
screen -dmS sniper-exec-dry bash -lc "cd '$PROJECT_DIR' && source venv/bin/activate && bash run_strategy_exec_loop.sh >> logs/sniper-exec-dry.log 2>&1"
echo "✅ sniper-exec-dry started"
screen -ls
