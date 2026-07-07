#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHONPATH=$PWD python -m index_sniper.backtest.quant_multi_alpha capital-sweep \
  --symbol "${BT_V40_SYMBOL:-BTCUSDT}" \
  --years-list "${BT_V40_YEARS_LIST:-1,2,3,5}" \
  --capital-ratios "${BT_V40_CAPITAL_RATIOS:-0.30,0.70,1.00}" \
  --leverage "${BT_V40_LEVERAGE:-3}" \
  --profile "${BT_V40_PROFILE:-trend_volume}" \
  --trend-gate "${BT_V40_TREND_GATE:-ema80_240}" \
  --entry-threshold "${BT_V40_ENTRY_THRESHOLD:-55}" \
  --exit-threshold "${BT_V40_EXIT_THRESHOLD:-15}" \
  --max-hold-bars "${BT_V40_MAX_HOLD_BARS:-24}" \
  --atr-stop-mult "${BT_V40_ATR_STOP_MULT:-1.5}" \
  --atr-take-profit-mult "${BT_V40_ATR_TP_MULT:-3.0}"
