#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
tail -f logs/v52-vwap-top10.log
