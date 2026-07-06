#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
python -m index_sniper.backtest.larry_trend_filter run \
  --symbols "${BT_V33_SYMBOLS:-BTCUSDT}" \
  --interval "${BT_V33_INTERVAL:-1H}" \
  --years "${BT_V33_YEARS_ONE:-5}" \
  --leverage "${BT_V33_LEVERAGE:-5}" \
  --capital-ratio "${BT_V33_CAPITAL_RATIO:-0.30}" \
  --trend-profile "${BT_V33_TREND_PROFILE:-4H_20_60}" \
  --k "${BT_V33_K:-0.50}"
