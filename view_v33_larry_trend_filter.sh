#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
for f in \
  backtests/v33_larry_trend_filter/larry_trend_filter_trend_sweep_latest.txt \
  backtests/v33_larry_trend_filter/larry_trend_filter_k_sweep_latest.txt \
  backtests/v33_larry_trend_filter/larry_trend_filter_sweep_latest.txt \
  backtests/v33_larry_trend_filter/larry_trend_filter_capital_sweep_latest.txt \
  backtests/v33_larry_trend_filter/larry_trend_filter_summary_latest.txt; do
  if [ -f "$f" ]; then
    echo
    echo "===== $f ====="
    cat "$f"
  fi
done
