#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== snapshots tail ====="
tail -n 20 research/signal_lab_snapshots.csv 2>/dev/null || true
echo
echo "===== signals tail ====="
tail -n 20 research/signal_lab_signals.csv 2>/dev/null || true
echo
echo "===== paper trades tail ====="
tail -n 40 research/signal_lab_paper_trades.csv 2>/dev/null || true
echo
echo "===== report ====="
cat research/signal_lab_report_latest.txt 2>/dev/null || true
