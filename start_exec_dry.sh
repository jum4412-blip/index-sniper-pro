#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! grep -q '^DRY_RUN=true' .env; then
  echo "🛑 DRY_RUN=true 상태에서만 start_exec_dry.sh를 실행하세요."
  exit 1
fi
bash stop_sniper.sh >/dev/null 2>&1 || true
mkdir -p logs data
: > logs/sniper-exec-dry.log
screen -dmS sniper-exec-dry bash -lc 'cd ~/index-sniper-pro && source venv/bin/activate && bash run_strategy_exec_loop.sh >> logs/sniper-exec-dry.log 2>&1'
echo "✅ sniper-exec-dry started"
screen -ls
