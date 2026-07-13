#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v systemctl >/dev/null 2>&1 && systemctl cat index-sniper-v63.service >/dev/null 2>&1; then
  sudo systemctl stop index-sniper-v63.service || true
fi
if command -v screen >/dev/null 2>&1; then
  screen -S btc-eth-v63 -X quit >/dev/null 2>&1 || true
fi
pkill -f 'python(3)? .*index_sniper[.]dual_live_v63 loop' >/dev/null 2>&1 || true
rm -f "$ROOT/data/v63_dual_live/nohup.pid"
echo "⏹️ v6.3 엔진 중지"
echo "주의: 엔진 중지는 포지션 청산이 아닙니다. 열린 포지션은 거래소에서 확인하세요."
