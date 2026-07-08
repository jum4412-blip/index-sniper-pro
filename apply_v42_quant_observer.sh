#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs data backtests
cp .env .env.bak.v42.$(date +%Y%m%d_%H%M%S) 2>/dev/null || true
for k in \
  V42_SYMBOL V42_CATEGORY V42_INTERVAL_SIGNAL V42_INTERVAL_TREND V42_LOOP_SECONDS V42_CANDLE_LIMIT_1H V42_CANDLE_LIMIT_4H \
  V42_STATE_PATH V42_JSONL_PATH V42_LOG_PATH V42_NOTIFY_NEUTRAL V42_NOTIFY_EVALS V42_NOTIFY_EVERY_MINUTES \
  V42_CONFIRM_COUNT V42_SIGNAL_COOLDOWN_MINUTES V42_WEAK_LONG_SCORE V42_STRONG_LONG_SCORE V42_WEAK_SHORT_SCORE V42_STRONG_SHORT_SCORE \
  V42_ATR_PENALTY_START_PCT V42_ATR_PENALTY_HARD_PCT V42_TREND_DYNAMIC_ENABLED V42_PRESSURE_ENABLED V42_SCORE_PROFILE; do
  sed -i "/^${k}=/d" .env 2>/dev/null || true
done
cat >> .env <<'EOF'

# ===== v4.2 BTC Quant Observer Upgrade =====
# Observation only. No orders. Dynamic trend haircut, short pressure logic, and signal performance tracking.
V42_SYMBOL=BTCUSDT
V42_CATEGORY=USDT-FUTURES
V42_INTERVAL_SIGNAL=1H
V42_INTERVAL_TREND=4H
V42_LOOP_SECONDS=900
V42_CANDLE_LIMIT_1H=360
V42_CANDLE_LIMIT_4H=240

V42_NOTIFY_NEUTRAL=false
V42_NOTIFY_EVALS=true
V42_NOTIFY_EVERY_MINUTES=120
V42_CONFIRM_COUNT=2
V42_SIGNAL_COOLDOWN_MINUTES=60

# Observation thresholds. These are not live-order thresholds.
V42_WEAK_LONG_SCORE=40
V42_STRONG_LONG_SCORE=60
V42_WEAK_SHORT_SCORE=-30
V42_STRONG_SHORT_SCORE=-55

V42_TREND_DYNAMIC_ENABLED=true
V42_PRESSURE_ENABLED=true
V42_ATR_PENALTY_START_PCT=1.20
V42_ATR_PENALTY_HARD_PCT=2.20
V42_SCORE_PROFILE=balanced

V42_STATE_PATH=data/quant_v42_observer_state.json
V42_JSONL_PATH=data/quant_v42_observer_events.jsonl
V42_LOG_PATH=logs/quant-v42.log
EOF
chmod +x run_quant_v42_once.sh start_quant_v42.sh stop_quant_v42.sh status_quant_v42.sh view_quant_v42_log.sh summarize_quant_v42.sh 2>/dev/null || true
# Keep __version__ available for main.py even when prior hotfixes changed it.
grep -q "__version__" index_sniper/__init__.py 2>/dev/null || echo '__version__ = "v4.2-local"' >> index_sniper/__init__.py

echo "✅ v4.2 BTC Quant Observer upgrade applied."
echo "관찰 전용입니다. 주문 파일/실전 주문 로직은 건드리지 않습니다."
