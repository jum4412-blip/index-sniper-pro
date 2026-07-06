#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHONPATH="$PWD" python -m index_sniper.backtest.larry_pure sweep \
  --symbols "${BT_LARRY_SYMBOLS:-BTCUSDT}" \
  --years "${BT_LARRY_YEARS:-1,2,3,4,5}" \
  --leverages "${BT_LARRY_LEVERAGES:-1,2,3,4,5,6,7,8,9,10}" \
  --k "${BT_LARRY_K:-0.50}" \
  --capital-ratio "${BT_LARRY_CAPITAL_RATIO:-0.30}" \
  --initial-equity "${BT_LARRY_INITIAL_EQUITY:-1374}" \
  --max-notional "${BT_LARRY_MAX_NOTIONAL:-999999}" \
  --both-mode "${BT_LARRY_BOTH_MODE:-stronger}" \
  --exit-modes "${BT_LARRY_EXIT_MODES:-next_open,open_stop_conservative,close_fail}" \
  --fee-rate "${BT_LARRY_FEE_RATE:-0.0006}" \
  --slippage-bps "${BT_LARRY_SLIPPAGE_BPS:-2.0}"
