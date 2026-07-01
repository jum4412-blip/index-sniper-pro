#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source venv/bin/activate
# Force dry-run for this preflight even if .env has DRY_RUN=false.
DRY_RUN=true python main.py --mode strategy-exec
