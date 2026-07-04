#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -d .venv ]; then
  source .venv/bin/activate
elif [ -d venv ]; then
  source venv/bin/activate
else
  echo "🛑 가상환경(.venv 또는 venv)을 찾을 수 없습니다. python3 -m venv .venv 후 설치가 필요합니다."
  exit 1
fi
# Force dry-run for this preflight even if .env has DRY_RUN=false.
DRY_RUN=true python main.py --mode strategy-exec
