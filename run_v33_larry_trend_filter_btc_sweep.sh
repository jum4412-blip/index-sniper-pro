#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
python -m index_sniper.backtest.larry_trend_filter sweep \
  --symbols "${BT_V33_SYMBOLS:-BTCUSDT}" \
  --interval "${BT_V33_INTERVAL:-1H}" \
  --years "${BT_V33_YEARS:-1,2,3,4,5}" \
  --capital-ratio "${BT_V33_CAPITAL_RATIO:-0.30}" \
  --trend-profile "${BT_V33_TREND_PROFILE:-4H_20_60}" \
  --k "${BT_V33_K:-0.50}" \
  --leverages "${BT_V33_LEVERAGES:-1,2,3,4,5,6,7,8,9,10}"
