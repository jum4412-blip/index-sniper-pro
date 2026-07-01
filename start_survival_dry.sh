#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
bash stop_sniper.sh >/dev/null 2>&1 || true
mkdir -p logs data
screen -dmS sniper-survival-dry bash -lc 'cd ~/index-sniper-pro && source venv/bin/activate && RISK_PROFILE=SURVIVAL DRY_RUN=true bash run_strategy_exec_loop.sh >> logs/sniper-survival-dry.log 2>&1'
echo "✅ sniper-survival-dry started"
screen -ls
