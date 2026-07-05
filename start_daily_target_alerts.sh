#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs data
LOOP_SECONDS="${DAILY_TARGET_ALERT_LOOP_SECONDS:-1800}"
screen -S sniper-targets -X quit >/dev/null 2>&1 || true
screen -dmS sniper-targets bash -lc "cd ~/index-sniper-pro && while true; do bash run_daily_targets.sh --once >> logs/daily-targets.log 2>&1; sleep ${LOOP_SECONDS}; done"
echo "✅ sniper-targets daily target alert loop started"
screen -ls
