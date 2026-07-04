#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== latest backtest summary ====="
cat backtests/backtest_summary_latest.txt 2>/dev/null || echo "No backtests/backtest_summary_latest.txt yet"
echo ""
echo "===== latest trades tail ====="
tail -n 30 backtests/trades_latest.csv 2>/dev/null || true
echo ""
echo "===== latest equity tail ====="
tail -n 20 backtests/equity_curve_latest.csv 2>/dev/null || true
