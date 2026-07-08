#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs data research
if screen -ls | grep -q "sniper-signal-lab"; then
  echo "sniper-signal-lab already running"
  exit 0
fi
screen -dmS sniper-signal-lab bash -lc 'cd ~/index-sniper-pro && source .venv/bin/activate && python -m index_sniper.signal_lab loop >> logs/signal-lab.log 2>&1'
echo "started sniper-signal-lab"
