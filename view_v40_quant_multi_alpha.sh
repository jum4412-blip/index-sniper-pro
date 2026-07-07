#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
for f in \
  backtests/v40_quant_multi_alpha/quant_multi_alpha_search_latest.txt \
  backtests/v40_quant_multi_alpha/quant_multi_alpha_robust_latest.txt \
  backtests/v40_quant_multi_alpha/quant_multi_alpha_capital_sweep_latest.txt \
  backtests/v40_quant_multi_alpha/quant_multi_alpha_summary_latest.txt
  do
    if [ -f "$f" ]; then
      echo
      echo "===== $f ====="
      cat "$f"
    fi
  done
