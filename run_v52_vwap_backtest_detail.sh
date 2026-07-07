#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="${PYTHONPATH:-$PWD}"
python -m index_sniper.backtest.vwap_top10_backtest detail \
  --days "${BT_V52_DAYS:-30}" \
  --interval "${BT_V52_INTERVAL:-1m}" \
  --leverage "${BT_V52_LEVERAGE:-1}"
