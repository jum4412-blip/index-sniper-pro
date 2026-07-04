#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "Created .env from .env.example. API keys still need to be filled for live trading."
  else
    touch .env
  fi
fi
cp .env ".env.bak.btcopt.$(date +%Y%m%d_%H%M%S)"
# Clean obvious pasted/non-dotenv lines first. This fixes Python-dotenv parse warnings from accidental pasted text.
bash clean_dotenv_parse_errors.sh >/dev/null || true
set_kv() {
  local key="$1"
  local value="$2"
  if grep -qE "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf "\n%s=%s\n" "$key" "$value" >> .env
  fi
}
# Backtest-only BTC profile. This does NOT change live CAPITAL_RATIO.
set_kv BT_SYMBOLS BTCUSDT
set_kv BT_INITIAL_EQUITY 1374
set_kv BT_CAPITAL_RATIO 0.30
set_kv BT_LEVERAGE 5
# Very high cap so 1x..10x leverage sweep is not flattened by a 1000 USDT notional cap.
set_kv BT_MAX_ORDER_NOTIONAL_USDT 1000
set_kv BT_OPT_MAX_ORDER_NOTIONAL_USDT 999999
set_kv BT_K_VALUE 0.50
set_kv BT_EMA_FAST 20
set_kv BT_EMA_SLOW 60
set_kv BT_USE_EMA_FILTER true
set_kv BT_NO_MA_BOTH_BREAKOUT_MODE skip
set_kv BT_ATR_STOP_MULT 1.30
set_kv BT_ATR_TAKE_PROFIT_MULT 2.00
set_kv BT_ANTI_CHASE_ENABLED true

echo "===== BTC optimizer backtest settings ====="
grep -E 'BT_SYMBOLS|BT_INITIAL_EQUITY|BT_CAPITAL_RATIO|BT_LEVERAGE|BT_MAX_ORDER_NOTIONAL_USDT|BT_OPT_MAX_ORDER_NOTIONAL_USDT|BT_K_VALUE|BT_EMA_FAST|BT_EMA_SLOW|BT_USE_EMA_FILTER|BT_NO_MA_BOTH_BREAKOUT_MODE|BT_ATR_STOP_MULT|BT_ATR_TAKE_PROFIT_MULT|BT_ANTI_CHASE_ENABLED' .env
