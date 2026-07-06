#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p index_sniper/backtest backtests/v33_larry_trend_filter
chmod +x run_v33_larry_trend_filter_btc_*.sh view_v33_larry_trend_filter.sh 2>/dev/null || true
python -m py_compile index_sniper/backtest/larry_trend_filter.py
echo "✅ v3.3 Larry trend-filter backtest patch applied"
echo "실전 봇은 건드리지 않습니다. 백테스트만 실행하세요."
echo "다음: bash run_v33_larry_trend_filter_btc_trend_sweep.sh"
