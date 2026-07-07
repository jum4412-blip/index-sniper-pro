#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
if screen -list | grep -q "v52-vwap"; then
  echo "v52-vwap already running"
  exit 0
fi
screen -dmS v52-vwap bash -lc 'cd ~/index-sniper-pro && source .venv/bin/activate && PYTHONPATH=$PWD python -m index_sniper.v52_vwap_top10 loop >> logs/v52-vwap-top10.log 2>&1'
echo "started screen: v52-vwap"
