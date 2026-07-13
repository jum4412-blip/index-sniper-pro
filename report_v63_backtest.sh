#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
mapfile -t FILES < <(find reports/v63_backtest -type f -name summary.txt -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -10 | cut -d' ' -f2-)
if [[ "${#FILES[@]}" -eq 0 ]]; then
  echo "백테스트 결과 없음. 먼저: bash run_v63_backtest.sh 60 1000 suite"
  exit 1
fi
for f in "${FILES[@]}"; do
  echo
  echo "### $f"
  cat "$f"
done
