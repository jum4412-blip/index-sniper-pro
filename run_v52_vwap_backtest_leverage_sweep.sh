#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="${PYTHONPATH:-$PWD}"
python -m index_sniper.backtest.vwap_top10_backtest sweep \
  --days "${BT_V52_DAYS:-30}" \
  --interval "${BT_V52_INTERVAL:-1m}" \
  --leverages "${BT_V52_LEVERAGES:-1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20}"
