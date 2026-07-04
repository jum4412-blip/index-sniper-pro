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
if [ -d .venv ]; then
  source .venv/bin/activate
elif [ -d venv ]; then
  source venv/bin/activate
else
  echo "🛑 가상환경(.venv 또는 venv)을 찾을 수 없습니다. python3 -m venv .venv 후 설치가 필요합니다."
  exit 1
fi
PYTHONUNBUFFERED=1 python -u main.py --mode strategy-exec-loop
