#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p index_sniper/backtest backtests/v32_larry_fail_exit
chmod +x run_v32_larry_fail_exit_btc_*.sh view_v32_larry_fail_exit.sh 2>/dev/null || true
python -m py_compile index_sniper/backtest/larry_fail_exit.py
echo "✅ v3.2 Larry first-touch + fail-exit backtest patch ready"
echo "실전 봇은 변경하지 않습니다. 백테스트 전용입니다."
echo "다음: bash run_v32_larry_fail_exit_btc_exit_sweep.sh"
