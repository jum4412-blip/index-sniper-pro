#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHONPATH=$PWD python -m index_sniper.research.quant_data_observer view \
  --symbol "${BT_V41_SYMBOL:-BTCUSDT}" \
  --tail "${BT_V41_TAIL:-20}"
