#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHONPATH=$PWD python -m index_sniper.backtest.larry_fail_exit sweep \
  --symbols "${BT_V32_SYMBOLS:-BTCUSDT}" \
  --interval "${BT_V32_INTERVAL:-1H}" \
  --years "${BT_V32_YEARS:-1,2,3,4,5}" \
  --k "${BT_V32_K:-0.5}" \
  --capital-ratio "${BT_V32_CAPITAL_RATIO:-0.30}" \
  --same-candle-mode "${BT_V32_SAME_CANDLE_MODE:-skip}" \
  --exit-mode "${BT_V32_EXIT_MODE:-target_reclaim_close}" \
  --leverages "${BT_V32_LEVERAGES:-1,2,3,4,5,6,7,8,9,10}"
