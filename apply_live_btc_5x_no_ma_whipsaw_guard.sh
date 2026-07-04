#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "🛑 .env 파일이 없습니다. 기존 .env를 먼저 준비하세요."
  exit 1
fi

cp .env ".env.bak.btc5x_whipsaw.$(date +%Y%m%d_%H%M%S)"

set_kv() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '\n%s=%s\n' "$key" "$value" >> .env
  fi
}

# Live candidate: BTC only / No-MA / long-short / 5x / 30% capital.
# This script intentionally DOES NOT force DRY_RUN=false or LIVE_TRADING_ENABLED=true.
# Run preflight first, then switch those live keys yourself when ready.
set_kv SYMBOLS "BTCUSDT"
set_kv LIVE_ALLOWED_SYMBOLS "BTCUSDT"
set_kv EXTERNAL_SIGNAL_ENABLED "false"
set_kv INDEX_WEEKEND_FLAT "false"
set_kv INDEX_WEEKEND_FLAT_SYMBOLS ""

set_kv LEVERAGE "5"
set_kv CAPITAL_RATIO "0.30"
set_kv MAX_LIVE_CAPITAL_RATIO "0.30"
set_kv MAX_OPEN_POSITIONS "1"
set_kv SURVIVAL_MAX_LIVE_OPEN_POSITIONS "1"
set_kv MAX_NEW_POSITIONS_PER_CYCLE "1"
set_kv MAX_DAILY_ENTRIES_PER_SYMBOL "1"
# Defense: do not fully compound after account grows. Adjust manually only after review.
set_kv MAX_ORDER_NOTIONAL_USDT "2500"

set_kv RISK_PROFILE "SURVIVAL"
set_kv USE_EMA_FILTER "false"
set_kv NO_MA_BOTH_BREAKOUT_MODE "stronger"
set_kv K_VALUE "0.50"
set_kv ATR_STOP_MULT "1.30"
set_kv ATR_TAKE_PROFIT_MULT "2.00"
# Stronger breakout confirmation than backtest baseline 0.05 ATR.
set_kv SURVIVAL_MIN_BREAKOUT_ATR "0.15"
set_kv MAX_ENTRY_EXTENSION_ATR "0.30"

# Anti-chase tightened for 5x.
set_kv ANTI_CHASE_ENABLED "true"
set_kv ANTI_CHASE_SYMBOLS "BTCUSDT"
set_kv ANTI_CHASE_EXTREME_UP_PCT "6.0"
set_kv ANTI_CHASE_EXTREME_DOWN_PCT "6.0"
set_kv ANTI_CHASE_EXTREME_RANGE_ATR "1.5"
set_kv ANTI_CHASE_EXTREME_LONG_SIZE_MULTIPLIER "0.0"
set_kv ANTI_CHASE_EXTREME_SHORT_SIZE_MULTIPLIER "0.0"

# v2.6 whipsaw/chop filter: block weak directional progress and frequent close-to-close flips.
set_kv WHIPSAW_FILTER_ENABLED "true"
set_kv WHIPSAW_FILTER_SYMBOLS "BTCUSDT"
set_kv WHIPSAW_FILTER_LOOKBACK_DAYS "10"
set_kv WHIPSAW_MIN_EFFICIENCY_RATIO "0.22"
set_kv WHIPSAW_MAX_FLIP_RATIO "0.60"

# Drawdown guards tightened for 5x.
set_kv DAILY_LOSS_GUARD_ENABLED "true"
set_kv MAX_DAILY_LOSS_PCT "3.00"
set_kv LIVE_GUARD_ENABLED "true"
set_kv LIVE_GUARD_STATE_PATH "data/live_guard_v26.json"
set_kv LIVE_GUARD_DRAWDOWN_WARN_PCT "10.0"
set_kv LIVE_GUARD_MONTHLY_LOSS_BLOCK_PCT "8.0"
set_kv LIVE_GUARD_MDD_BLOCK_PCT "18.0"

set_kv POSITION_MANAGER_ENABLED "true"
set_kv POSITION_MAX_HOLD_HOURS_BTC "72"
set_kv POSITION_BREAKEVEN_ALERT_R "1.0"
set_kv POSITION_MANAGER_AUTO_CLOSE "false"

# Keep live switch manual.
if ! grep -q '^DRY_RUN=' .env; then set_kv DRY_RUN "true"; fi
if ! grep -q '^LIVE_TRADING_ENABLED=' .env; then set_kv LIVE_TRADING_ENABLED "false"; fi

cat <<'EOF'
✅ BTC 5x No-MA Whipsaw Guard 설정 적용 완료

중요:
- 이 스크립트는 DRY_RUN=false를 강제로 켜지 않습니다.
- 먼저 bash run_live_preflight.sh 로 신호/체크를 확인하세요.
- 실전 시작 전 Bitget BTCUSDT 레버리지가 5x인지 확인하세요.
EOF

grep -E '^(DRY_RUN|LIVE_TRADING_ENABLED|SYMBOLS|LEVERAGE|CAPITAL_RATIO|MAX_LIVE_CAPITAL_RATIO|MAX_ORDER_NOTIONAL_USDT|USE_EMA_FILTER|NO_MA_BOTH_BREAKOUT_MODE|SURVIVAL_MIN_BREAKOUT_ATR|MAX_ENTRY_EXTENSION_ATR|ANTI_CHASE|WHIPSAW_|LIVE_GUARD_|MAX_DAILY_LOSS_PCT|MAX_OPEN_POSITIONS|MAX_DAILY_ENTRIES_PER_SYMBOL)=' .env || true
