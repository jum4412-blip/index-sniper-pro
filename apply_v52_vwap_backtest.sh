#!/usr/bin/env bash
set -euo pipefail
mkdir -p backtests/v52_vwap_top10/data
chmod +x run_v52_vwap_backtest_leverage_sweep.sh run_v52_vwap_backtest_detail.sh 2>/dev/null || true
echo "✅ v5.2 VWAP Top10 backtest patch applied."
echo "실전 봇 파일은 건드리지 않습니다. 백테스트 전용입니다."
