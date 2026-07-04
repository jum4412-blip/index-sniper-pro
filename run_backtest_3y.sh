#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source venv/bin/activate
python -m index_sniper.backtest.runner --years 3 "$@"
