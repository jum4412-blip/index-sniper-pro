#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p local_backups backtests/v30_larry_pure backtests/data
if [ -f .env ]; then
  cp .env "local_backups/.env.bak.v30_larry.$(date +%Y%m%d_%H%M%S)"
fi

set_env() {
  local key="$1"
  local value="$2"
  if [ -f .env ]; then
    sed -i "/^${key}=/d" .env
  fi
  printf '%s=%s\n' "$key" "$value" >> .env
}

# v3.0 Larry Pure Backtest defaults. This script does NOT start live trading.
set_env BT_LARRY_SYMBOLS BTCUSDT
set_env BT_LARRY_INITIAL_EQUITY "${BT_INITIAL_EQUITY:-1374}"
set_env BT_LARRY_CAPITAL_RATIO "${BT_CAPITAL_RATIO:-0.30}"
set_env BT_LARRY_MAX_NOTIONAL "${BT_OPT_MAX_ORDER_NOTIONAL_USDT:-999999}"
set_env BT_LARRY_K "${K_VALUE:-0.50}"
set_env BT_LARRY_LEVERAGE "${LEVERAGE:-5}"
set_env BT_LARRY_LEVERAGES "1,2,3,4,5,6,7,8,9,10"
set_env BT_LARRY_YEARS "1,2,3,4,5"
set_env BT_LARRY_EXIT_MODES "next_open,open_stop_conservative,close_fail"
set_env BT_LARRY_BOTH_MODE stronger
set_env BT_LARRY_K_VALUES "0.25,0.35,0.50,0.65,0.80,1.00"
set_env BT_LARRY_FEE_RATE 0.0006
set_env BT_LARRY_SLIPPAGE_BPS 2.0

chmod +x run_v30_larry_pure_btc_sweep.sh run_v30_larry_pure_btc_k_sweep.sh run_v30_larry_pure_btc_detail.sh view_v30_larry_pure.sh 2>/dev/null || true

echo "✅ v3.0 Larry Pure backtest settings applied"
echo "실전 봇은 건드리지 않았습니다. 백테스트만 실행하세요."
echo "다음: bash run_v30_larry_pure_btc_sweep.sh"
