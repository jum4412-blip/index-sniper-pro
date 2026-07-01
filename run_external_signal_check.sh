#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source venv/bin/activate
mkdir -p logs
# Force DRY for this diagnostic even when live mode is enabled in .env.
DRY_RUN=true EXTERNAL_SIGNAL_ENABLED=true python main.py --mode strategy-exec | tee logs/external_signal_latest.log
