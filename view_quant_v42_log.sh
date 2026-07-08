#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
touch logs/quant-v42.log
tail -f logs/quant-v42.log
