#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PARTIAL_PROFILE="${PARTIAL_PROFILE:-p50_be_25}"
export SIDE_MODE="${SIDE_MODE:-ls}"
export BOTH_MODE="${BOTH_MODE:-stronger}"
export LEVERAGES="${LEVERAGES:-1-10}"
PYTHONPATH="$PWD" python -m index_sniper.backtest.partial_exit final "$@"
