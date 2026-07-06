#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHONPATH="$PWD" python -m index_sniper.backtest.larry_first_touch ksweep \
  --symbols "${BT_FT_SYMBOLS:-BTCUSDT}" \
  --interval "${BT_FT_INTERVAL:-1H}" \
  --years "${BT_FT_YEARS:-1,2,3,4,5}" \
  --leverage "${BT_FT_LEVERAGE:-5}" \
  --k-values "${BT_FT_K_VALUES:-0.25,0.35,0.50,0.65,0.80,1.00}" \
  --capital-ratio "${BT_FT_CAPITAL_RATIO:-0.30}" \
  --initial-equity "${BT_FT_INITIAL_EQUITY:-1374}" \
  --max-notional "${BT_FT_MAX_NOTIONAL:-999999}" \
  --same-candle-mode "${BT_FT_SAME_CANDLE_MODE:-skip}" \
  --min-bars-per-day "${BT_FT_MIN_BARS_PER_DAY:-20}" \
  --fee-rate "${BT_FT_FEE_RATE:-0.0006}" \
  --slippage-bps "${BT_FT_SLIPPAGE_BPS:-2.0}"
