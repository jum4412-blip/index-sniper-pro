#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
python -m index_sniper.backtest.larry_trend_filter trend-sweep \
  --symbols "${BT_V33_SYMBOLS:-BTCUSDT}" \
  --interval "${BT_V33_INTERVAL:-1H}" \
  --years "${BT_V33_YEARS:-1,2,3,4,5}" \
  --leverage "${BT_V33_LEVERAGE:-5}" \
  --capital-ratio "${BT_V33_CAPITAL_RATIO:-0.30}" \
  --k "${BT_V33_K:-0.50}" \
  --trend-profiles "${BT_V33_TREND_PROFILES:-none,1H_20_60,1H_50_200,4H_20_60,4H_50_200,1H_20_60+4H_20_60,1H_50_200+4H_50_200}"
