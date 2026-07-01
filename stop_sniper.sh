#!/usr/bin/env bash
set +e
for s in $(screen -ls | awk '/sniper-exec-dry|sniper-live|sniper/ {print $1}'); do
  screen -S "$s" -X quit
  echo "stopped screen $s"
done
pkill -f 'python main.py --mode strategy-exec-loop' 2>/dev/null || true
pkill -f 'run_strategy_exec_loop.sh' 2>/dev/null || true
screen -wipe >/dev/null 2>&1 || true
echo "✅ sniper stopped"
screen -ls || true
