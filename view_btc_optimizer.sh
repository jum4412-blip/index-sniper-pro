#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== latest BTC optimizer summary ====="
cat backtests/btc_optimizer_latest.txt 2>/dev/null || echo "No btc optimizer summary yet."
echo
echo "===== latest BTC optimizer 3y/5y comparison ====="
cat backtests/btc_optimizer_compare_latest.txt 2>/dev/null || echo "No btc optimizer comparison yet."
echo
echo "===== top CSV rows ====="
head -n 20 backtests/btc_optimizer_latest.csv 2>/dev/null || true
