#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs data
for s in $(screen -ls | awk '/quant-v42/ {print $1}'); do screen -S "$s" -X quit || true; done
screen -dmS quant-v42 bash -lc 'cd ~/index-sniper-pro && source .venv/bin/activate && python -m index_sniper.v42_quant_observer loop >> logs/quant-v42.screen.log 2>&1'
echo "started screen: quant-v42"
screen -ls | grep quant-v42 || true
