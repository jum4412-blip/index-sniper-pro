#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
mkdir -p "$ROOT/logs" "$ROOT/data/v63_dual_live"
if pgrep -af 'python(3)? .*index_sniper[.]dual_live_v63 loop' >/dev/null 2>&1; then
  echo "✅ v6.3 loop 이미 실행 중"
  pgrep -af 'python(3)? .*index_sniper[.]dual_live_v63 loop' || true
  exit 0
fi
if command -v systemctl >/dev/null 2>&1 && systemctl cat index-sniper-v63.service >/dev/null 2>&1; then
  sudo systemctl start index-sniper-v63.service
  sleep 2
  sudo systemctl --no-pager --full status index-sniper-v63.service | sed -n '1,18p' || true
  exit 0
fi
if command -v screen >/dev/null 2>&1; then
  screen -dmS btc-eth-v63 bash -lc "cd '$ROOT'; export PYTHONPATH='$ROOT'; exec '$PY' -m index_sniper.dual_live_v63 loop >> '$ROOT/logs/v63-dual-live.log' 2>&1"
  sleep 2
  echo "✅ v6.3 screen 시작: btc-eth-v63"
else
  nohup env PYTHONPATH="$ROOT" "$PY" -m index_sniper.dual_live_v63 loop >> "$ROOT/logs/v63-dual-live.log" 2>&1 &
  echo $! > "$ROOT/data/v63_dual_live/nohup.pid"
  sleep 2
  echo "✅ v6.3 nohup 시작: PID $(cat "$ROOT/data/v63_dual_live/nohup.pid")"
fi
