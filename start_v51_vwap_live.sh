#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
if screen -list | grep -q "v51-vwap"; then
  echo "v51-vwap already running"
  exit 0
fi
screen -dmS v51-vwap bash -lc 'cd ~/index-sniper-pro && source .venv/bin/activate && PYTHONPATH=$PWD python -m index_sniper.v51_vwap_top20 loop >> logs/v51-vwap-top20.log 2>&1'
echo "started screen: v51-vwap"
