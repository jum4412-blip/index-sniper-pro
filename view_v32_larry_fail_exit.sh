#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
for f in \
  backtests/v32_larry_fail_exit/larry_fail_exit_exit_sweep_latest.txt \
  backtests/v32_larry_fail_exit/larry_fail_exit_k_sweep_latest.txt \
  backtests/v32_larry_fail_exit/larry_fail_exit_sweep_latest.txt \
  backtests/v32_larry_fail_exit/larry_fail_exit_capital_sweep_latest.txt \
  backtests/v32_larry_fail_exit/larry_fail_exit_summary_latest.txt; do
  if [[ -f "$f" ]]; then
    echo
    echo "===== $f ====="
    cat "$f"
  fi
done
