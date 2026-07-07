#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs data/quant_v41
screen -S quant-v41 -X quit >/dev/null 2>&1 || true
screen -dmS quant-v41 bash -lc 'cd "$PWD"; source .venv/bin/activate 2>/dev/null || true; PYTHONPATH=$PWD python -m index_sniper.research.quant_data_observer loop --symbol "${BT_V41_SYMBOL:-BTCUSDT}" --interval "${BT_V41_INTERVAL:-1H}" --candle-limit "${BT_V41_CANDLE_LIMIT:-500}" --minutes "${BT_V41_LOOP_MINUTES:-15}" >> logs/quant-v41.log 2>&1'
echo "✅ v4.1 quant observer started: screen quant-v41"
echo "log: tail -f logs/quant-v41.log"
