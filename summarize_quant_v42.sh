#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python -m index_sniper.v42_quant_observer summary | tee backtests/quant_v42_observer_summary_latest.txt
