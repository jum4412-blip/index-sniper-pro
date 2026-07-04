#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -d .venv ]; then
  source .venv/bin/activate
elif [ -d venv ]; then
  source venv/bin/activate
else
  echo "🛑 가상환경(.venv 또는 venv)을 찾을 수 없습니다."
  exit 1
fi
PYTHONPATH=$PWD DRY_RUN=true python main.py --mode strategy-exec
