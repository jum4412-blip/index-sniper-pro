#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHONPATH=$PWD python -m index_sniper.backtest.larry_fail_exit capital-sweep \
  --symbols "${BT_V32_SYMBOLS:-BTCUSDT}" \
  --interval "${BT_V32_INTERVAL:-1H}" \
  --years "${BT_V32_YEARS:-1,2,3,4,5}" \
  --leverage "${BT_V32_LEVERAGE:-5}" \
  --k "${BT_V32_K:-0.5}" \
  --same-candle-mode "${BT_V32_SAME_CANDLE_MODE:-skip}" \
  --exit-mode "${BT_V32_EXIT_MODE:-target_reclaim_close}" \
  --capital-ratios "${BT_V32_CAPITAL_RATIOS:-0.30,0.70,1.00}"
