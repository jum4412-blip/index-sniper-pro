#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs data
source venv/bin/activate
# Force DRY_RUN for a one-shot observation snapshot. No real order can be placed by this script.
DRY_RUN=true PYTHONUNBUFFERED=1 python -u main.py --mode strategy-exec | tee logs/observer_snapshot.log
