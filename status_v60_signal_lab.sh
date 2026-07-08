#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== screen ====="
screen -ls | grep -E 'sniper-signal-lab|No Sockets' || screen -ls || true
echo
echo "===== latest state ====="
python -m index_sniper.signal_lab state || true
echo
echo "===== files ====="
ls -lh research/signal_lab_*.csv research/signal_lab_report_latest.txt data/signal_lab_state.json 2>/dev/null || true
