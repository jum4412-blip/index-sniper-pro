#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PRESET="${PRESET:-no_ma_quick}"
echo "===== Running BTC NO-MA optimizer 5y preset=$PRESET ====="
python3 -m index_sniper.backtest.btc_optimizer --years 5 --preset "$PRESET" "$@"
echo "===== Running BTC NO-MA optimizer 3y preset=$PRESET ====="
python3 -m index_sniper.backtest.btc_optimizer --years 3 --preset "$PRESET" "$@"
echo "===== Comparing 3y and 5y ====="
python3 -m index_sniper.backtest.btc_optimizer_compare
