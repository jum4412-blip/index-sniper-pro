#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
export PYTHONPATH="$ROOT"

DAYS="${1:-60}"
EQUITY="${2:-1000}"
MODE="${3:-suite}"

run_one() {
  local oi_mode="$1"
  local confirm="$2"
  local fee="$3"
  local slip="$4"
  echo
  echo "===== V6.3 BACKTEST: oi=$oi_mode confirm=$confirm fee=$fee slip=$slip ====="
  "$PY" -m index_sniper.backtest_v63 \
    --days "$DAYS" \
    --initial-equity "$EQUITY" \
    --oi-mode "$oi_mode" \
    --impulse-confirm-bars "$confirm" \
    --fee-bps "$fee" \
    --slippage-bps "$slip"
}

case "$MODE" in
  suite)
    run_one conservative 1 6 3
    run_one proxy 1 6 3
    run_one conservative 2 8 5
    ;;
  conservative)
    run_one conservative 1 6 3
    ;;
  proxy)
    run_one proxy 1 6 3
    ;;
  stress)
    run_one conservative 2 8 5
    ;;
  *)
    echo "Usage: bash run_v63_backtest.sh [days] [initial_equity] [suite|conservative|proxy|stress]"
    exit 2
    ;;
esac

echo
echo "Latest summaries:"
find reports/v63_backtest -type f -name summary.txt -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -3 | cut -d' ' -f2- || true
