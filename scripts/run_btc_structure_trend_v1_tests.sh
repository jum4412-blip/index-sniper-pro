#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$(command -v python3)}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
"$PY" -m py_compile "$ROOT/index_sniper/btc_structure_trend_v1.py"
"$PY" -m index_sniper.btc_structure_trend_v1 --config "$ROOT/config/btc_structure_trend_v1.json" self-test
"$PY" -m unittest discover -s "$ROOT/tests" -v
