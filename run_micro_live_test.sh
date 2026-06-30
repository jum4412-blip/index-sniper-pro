#!/usr/bin/env bash
set -euo pipefail
if [[ "${LIVE_TEST_CONFIRM:-}" != "I_UNDERSTAND_REAL_ORDER" ]]; then
  echo "🛑 This script sends a REAL micro market order."
  echo "Run only when ready:"
  echo "DRY_RUN=false LIVE_TEST_CONFIRM=I_UNDERSTAND_REAL_ORDER LIVE_TEST_SYMBOL=BTCUSDT bash run_micro_live_test.sh"
  exit 1
fi
source venv/bin/activate
python main.py --mode micro-live-test
