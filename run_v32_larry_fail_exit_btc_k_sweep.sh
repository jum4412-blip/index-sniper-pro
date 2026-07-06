#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHONPATH=$PWD python -m index_sniper.backtest.larry_fail_exit ksweep \
  --symbols "${BT_V32_SYMBOLS:-BTCUSDT}" \
  --interval "${BT_V32_INTERVAL:-1H}" \
  --years "${BT_V32_YEARS:-1,2,3,4,5}" \
  --leverage "${BT_V32_LEVERAGE:-5}" \
  --capital-ratio "${BT_V32_CAPITAL_RATIO:-0.30}" \
  --same-candle-mode "${BT_V32_SAME_CANDLE_MODE:-skip}" \
  --exit-mode "${BT_V32_EXIT_MODE:-target_reclaim_close}" \
  --k-values "${BT_V32_K_VALUES:-0.25,0.35,0.50,0.65,0.80,1.00}"
