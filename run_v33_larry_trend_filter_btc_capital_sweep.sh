#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
python -m index_sniper.backtest.larry_trend_filter capital-sweep \
  --symbols "${BT_V33_SYMBOLS:-BTCUSDT}" \
  --interval "${BT_V33_INTERVAL:-1H}" \
  --years "${BT_V33_YEARS:-1,2,3,4,5}" \
  --leverage "${BT_V33_LEVERAGE:-5}" \
  --trend-profile "${BT_V33_TREND_PROFILE:-4H_20_60}" \
  --k "${BT_V33_K:-0.50}" \
  --capital-ratios "${BT_V33_CAPITAL_RATIOS:-0.30,0.70,1.00}"
