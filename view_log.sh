#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
touch logs/sniper-exec-dry.log
tail -f logs/sniper-exec-dry.log
