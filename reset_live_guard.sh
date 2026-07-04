#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
rm -f data/live_guard_v26.json
rm -f data/equity_guard.json
printf '✅ live guard / daily equity guard reset 완료\n'
