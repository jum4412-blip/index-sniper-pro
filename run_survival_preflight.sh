#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source venv/bin/activate
# Force dry-run for this survival preflight even if .env has DRY_RUN=false.
DRY_RUN=true RISK_PROFILE=SURVIVAL python main.py --mode strategy-exec
