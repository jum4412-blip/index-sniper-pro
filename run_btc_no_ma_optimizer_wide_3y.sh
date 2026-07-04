#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m index_sniper.backtest.btc_optimizer --years 3 --preset no_ma_wide "$@"
