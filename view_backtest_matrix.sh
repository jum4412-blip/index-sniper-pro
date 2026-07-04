#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== latest matrix summary ====="
cat backtests/backtest_matrix_latest.txt 2>/dev/null || echo "No backtests/backtest_matrix_latest.txt yet"
echo ""
echo "===== latest matrix csv top ====="
head -n 20 backtests/backtest_matrix_latest.csv 2>/dev/null || true
