#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f backtests/v30_larry_pure/larry_pure_sweep_latest.txt ]; then
  cat backtests/v30_larry_pure/larry_pure_sweep_latest.txt
fi
if [ -f backtests/v30_larry_pure/larry_pure_k_sweep_latest.txt ]; then
  echo
  cat backtests/v30_larry_pure/larry_pure_k_sweep_latest.txt
fi
if [ -f backtests/v30_larry_pure/larry_pure_summary_latest.txt ]; then
  echo
  cat backtests/v30_larry_pure/larry_pure_summary_latest.txt
fi
ls -lh backtests/v30_larry_pure 2>/dev/null || true
