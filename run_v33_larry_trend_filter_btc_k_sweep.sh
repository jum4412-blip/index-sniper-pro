#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
python -m index_sniper.backtest.larry_trend_filter ksweep \
  --symbols "${BT_V33_SYMBOLS:-BTCUSDT}" \
  --interval "${BT_V33_INTERVAL:-1H}" \
  --years "${BT_V33_YEARS:-1,2,3,4,5}" \
  --leverage "${BT_V33_LEVERAGE:-5}" \
  --capital-ratio "${BT_V33_CAPITAL_RATIO:-0.30}" \
  --trend-profile "${BT_V33_TREND_PROFILE:-4H_20_60}" \
  --k-values "${BT_V33_K_VALUES:-0.25,0.35,0.50,0.65,0.80,1.00}"
