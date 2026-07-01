#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== screen ====="
screen -ls || true
echo
echo "===== .env safety ====="
grep -E '^(DRY_RUN|SYMBOLS|LEVERAGE|CAPITAL_RATIO|STRATEGY_HEARTBEAT_MINUTES|LOOP_SECONDS)=' .env || true
echo
echo "===== loop status ====="
if [ -f data/loop_status.json ]; then
  cat data/loop_status.json
else
  echo "no data/loop_status.json yet"
fi
echo
echo "===== heartbeat log tail ====="
if [ -f logs/heartbeat.log ]; then
  tail -n 20 logs/heartbeat.log
else
  echo "no logs/heartbeat.log yet"
fi
echo
echo "===== main log tail ====="
if [ -f logs/sniper-exec-dry.log ]; then
  tail -n 40 logs/sniper-exec-dry.log
else
  echo "no logs/sniper-exec-dry.log yet"
fi
