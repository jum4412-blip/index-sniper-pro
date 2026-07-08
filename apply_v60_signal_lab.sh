#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

cp .env ".env.bak.v60.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true

# Remove previous v6 values to avoid duplicate confusion.
sed -i '/^V60_/d' .env 2>/dev/null || true

cat >> .env <<'EOF'

# ===== v6.0 BTC Quant Signal Lab / PAPER ONLY =====
V60_SYMBOL=BTCUSDT
V60_CATEGORY=USDT-FUTURES
V60_LOOP_SECONDS=300
V60_SNAPSHOT_INTERVAL_SECONDS=300

# Signal thresholds. These create PAPER entries only, never real orders.
V60_SIGNAL_THRESHOLD=70
V60_WEAK_THRESHOLD=50
V60_MIN_EDGE=15
V60_COOLDOWN_MINUTES=30
V60_MAX_ACTIVE_PER_SIDE=1

# Exit plans tested simultaneously for each paper signal.
V60_EXIT_PLANS=tp05_sl03_2h:0.005:0.003:120,tp08_sl04_4h:0.008:0.004:240,tp12_sl06_8h:0.012:0.006:480,tp20_sl08_12h:0.020:0.008:720

# Feature/scoring controls.
V60_FUNDING_WEIGHT=8
V60_OI_WEIGHT=8
V60_RISK_ATR_SOFT_PCT=1.2
V60_RISK_ATR_HARD_PCT=2.4

# Files.
V60_STATE_PATH=data/signal_lab_state.json
V60_SNAPSHOTS_PATH=research/signal_lab_snapshots.csv
V60_SIGNALS_PATH=research/signal_lab_signals.csv
V60_TRADES_PATH=research/signal_lab_paper_trades.csv
V60_EVENTS_PATH=research/signal_lab_events.jsonl
V60_REPORT_PATH=research/signal_lab_report_latest.txt
V60_REPORT_DAYS=14

# Telegram noise control.
V60_NOTIFY=true
V60_NOTIFY_SIGNALS=true
V60_NOTIFY_CLOSES=true
V60_NOTIFY_SUMMARY_MINUTES=60
EOF

mkdir -p data logs research
chmod +x run_v60_signal_lab_once.sh start_v60_signal_lab.sh stop_v60_signal_lab.sh status_v60_signal_lab.sh view_v60_signal_lab_log.sh run_v60_signal_lab_report.sh view_v60_signal_lab_files.sh 2>/dev/null || true

echo "✅ v6.0 BTC Quant Signal Lab patch applied."
echo "실주문 없음 / paper-only 연구실입니다."
