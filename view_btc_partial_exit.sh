#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== latest partial final ====="
cat backtests/btc_partial_final_latest.txt 2>/dev/null || true
echo
echo "===== latest partial risk ====="
cat backtests/btc_partial_risk_latest.txt 2>/dev/null || true
