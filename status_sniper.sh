#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== screen ====="
screen -ls || true
echo
echo "===== .env safety ====="
grep -E '^(DRY_RUN|SYMBOLS|LEVERAGE|CAPITAL_RATIO|RISK_PROFILE|MAX_OPEN_POSITIONS|MAX_DAILY_LOSS_PCT|SURVIVAL_|EXTERNAL_|STRATEGY_HEARTBEAT_MINUTES|LOOP_SECONDS)=' .env || true
echo
echo "===== loop status ====="
if [ -f data/loop_status.json ]; then
  cat data/loop_status.json
else
  echo "no data/loop_status.json yet"
fi
echo
echo "===== heartbeat log tail ====="
if [ -f logs/heartbeat.log ]; then
  tail -n 20 logs/heartbeat.log
else
  echo "no logs/heartbeat.log yet"
fi
echo
echo "===== survival dry log tail ====="
if [ -f logs/sniper-survival-dry.log ]; then
  tail -n 40 logs/sniper-survival-dry.log
else
  echo "no logs/sniper-survival-dry.log yet"
fi
echo
echo "===== exec dry log tail ====="
if [ -f logs/sniper-exec-dry.log ]; then
  tail -n 40 logs/sniper-exec-dry.log
else
  echo "no logs/sniper-exec-dry.log yet"
fi

echo
echo "===== observer snapshot ====="
if [ -f data/market_observer.json ]; then
  python - <<'PY'
import json
from pathlib import Path
p = Path('data/market_observer.json')
try:
    data = json.loads(p.read_text(encoding='utf-8'))
    print(f"updated_at: {data.get('updated_at')} | mode: {data.get('mode')} | dry_run: {data.get('dry_run')}")
    for obs in data.get('observations', []):
        print(f"- {obs.get('symbol')}: {obs.get('human')} | status={obs.get('status')} score={obs.get('survival_signal_score')}")
except Exception as e:
    print(f"observer snapshot parse error: {e}")
PY
else
  echo "no data/market_observer.json yet"
fi
