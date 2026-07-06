#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f backtests/v31_larry_first_touch/larry_first_touch_k_sweep_latest.txt ]; then
  cat backtests/v31_larry_first_touch/larry_first_touch_k_sweep_latest.txt
fi
if [ -f backtests/v31_larry_first_touch/larry_first_touch_sweep_latest.txt ]; then
  echo
  cat backtests/v31_larry_first_touch/larry_first_touch_sweep_latest.txt
fi
if [ -f backtests/v31_larry_first_touch/larry_first_touch_summary_latest.txt ]; then
  echo
  cat backtests/v31_larry_first_touch/larry_first_touch_summary_latest.txt
fi
ls -lh backtests/v31_larry_first_touch 2>/dev/null || true
