#!/usr/bin/env bash
set -euo pipefail
if ! grep -q '^DRY_RUN=false' .env; then
  echo "🛑 .env의 DRY_RUN=false가 필요합니다. 지금은 실주문이 차단됩니다."
  exit 1
fi
if ! grep -q '^STRATEGY_LIVE_CONFIRM=I_UNDERSTAND_AUTO_TRADING' .env; then
  echo "🛑 .env에 STRATEGY_LIVE_CONFIRM=I_UNDERSTAND_AUTO_TRADING 확인문구가 필요합니다."
  exit 1
fi
source venv/bin/activate
python main.py --mode strategy-exec-loop
