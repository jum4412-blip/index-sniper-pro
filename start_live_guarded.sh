#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! grep -q '^DRY_RUN=false' .env; then
  echo "🛑 .env의 DRY_RUN=false가 필요합니다. 지금은 실주문이 차단됩니다."
  exit 1
fi
if ! grep -q '^LIVE_TRADING_ENABLED=true' .env; then
  echo "🛑 .env에 LIVE_TRADING_ENABLED=true가 필요합니다."
  exit 1
fi
if ! grep -q '^STRATEGY_LIVE_CONFIRM=I_UNDERSTAND_AUTO_TRADING' .env; then
  echo "🛑 .env에 STRATEGY_LIVE_CONFIRM=I_UNDERSTAND_AUTO_TRADING 확인문구가 필요합니다."
  exit 1
fi
if ! grep -q '^LIVE_START_CONFIRM=START_LIVE_INDEX_SNIPER' .env; then
  echo "🛑 .env에 LIVE_START_CONFIRM=START_LIVE_INDEX_SNIPER 확인문구가 필요합니다."
  exit 1
fi
bash stop_sniper.sh >/dev/null 2>&1 || true
mkdir -p logs data
screen -dmS sniper-live bash -lc 'cd ~/index-sniper-pro && source venv/bin/activate && while true; do bash run_strategy_live_loop.sh >> logs/sniper-live.log 2>&1; echo "$(date -u +%FT%TZ) live loop exited; restarting in 30s" >> logs/sniper-live.log; sleep 30; done'
echo "✅ sniper-live started"
screen -ls
