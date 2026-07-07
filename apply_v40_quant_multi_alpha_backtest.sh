#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backtests/v40_quant_multi_alpha
python -m py_compile index_sniper/backtest/quant_multi_alpha.py
cat <<'EOF'
✅ v4.0 OHLCV Multi-Alpha Quant backtest patch applied.
실전 봇 파일은 변경하지 않습니다. 백테스트/리서치 전용입니다.
EOF
