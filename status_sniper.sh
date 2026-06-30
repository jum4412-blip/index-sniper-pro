#!/usr/bin/env bash
set -euo pipefail
echo "===== screens ====="
screen -ls || true
echo
echo "===== python/sniper processes ====="
ps -ef | grep -E 'sniper|strategy_exec|main.py' | grep -v grep || true
echo
echo "===== recent log ====="
if [ -f logs/sniper-exec-dry.log ]; then
  tail -n 40 logs/sniper-exec-dry.log
else
  echo "no log file"
fi
