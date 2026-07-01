#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! grep -q '^DRY_RUN=false' .env; then
  echo "🛑 .env의 DRY_RUN=false가 필요합니다."
  exit 1
fi
source venv/bin/activate
python main.py --mode strategy-exec
