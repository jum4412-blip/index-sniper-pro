#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHONPATH=$PWD python -m index_sniper.v52_vwap_top10 preflight
