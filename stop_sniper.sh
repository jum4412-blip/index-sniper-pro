#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
pkill -f run_strategy_exec_loop.sh 2>/dev/null || true
pkill -f run_strategy_live_loop.sh 2>/dev/null || true
pkill -f "python main.py --mode strategy-exec-loop" 2>/dev/null || true
pkill -f "python main.py --mode strategy-loop-dry" 2>/dev/null || true
if command -v screen >/dev/null 2>&1; then
  for s in $(screen -ls | awk '/sniper/ {print $1}'); do
    screen -S "$s" -X quit 2>/dev/null || true
  done
  screen -wipe 2>/dev/null || true
  screen -ls || true
fi
echo "✅ sniper processes stopped"
