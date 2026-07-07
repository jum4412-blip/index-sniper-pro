#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHONPATH=$PWD python -m index_sniper.research.quant_data_observer once \
  --symbol "${BT_V41_SYMBOL:-BTCUSDT}" \
  --interval "${BT_V41_INTERVAL:-1H}" \
  --candle-limit "${BT_V41_CANDLE_LIMIT:-500}" \
  ${BT_V41_NOTIFY_ONCE:+--notify}
