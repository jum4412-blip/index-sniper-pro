#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data/quant_v41 logs
chmod +x run_v41_quant_snapshot.sh start_v41_quant_observer.sh stop_v41_quant_observer.sh view_v41_quant_state.sh 2>/dev/null || true
python -m py_compile index_sniper/research/quant_data_observer.py
echo "✅ v4.1 Quant Data Observer patch applied."
echo "실주문 없음: OHLCV + funding + OI 관찰/점수화 전용입니다."
