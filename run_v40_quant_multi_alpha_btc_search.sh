#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHONPATH=$PWD python -m index_sniper.backtest.quant_multi_alpha search \
  --symbol "${BT_V40_SYMBOL:-BTCUSDT}" \
  --years "${BT_V40_YEARS:-5}" \
  --capital-ratio "${BT_V40_CAPITAL_RATIO:-0.30}" \
  --leverage "${BT_V40_LEVERAGE:-3}" \
  --positive-only
