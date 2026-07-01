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
# v0.9 watchdog wrapper: if Python exits/crashes, it restarts after 30 seconds.
screen -dmS sniper-exec-dry bash -lc 'cd ~/index-sniper-pro && source venv/bin/activate && while true; do echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting strategy exec loop"; PYTHONUNBUFFERED=1 python -u main.py --mode strategy-exec-loop; code=$?; echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] strategy loop exited code=$code; restarting in 30s"; sleep 30; done >> logs/sniper-exec-dry.log 2>&1'
echo "✅ sniper-exec-dry started with watchdog"
screen -ls
