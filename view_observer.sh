#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== latest observer snapshot ====="
if [ -f data/market_observer.json ]; then
  python - <<'PY'
import json
from pathlib import Path
p = Path('data/market_observer.json')
data = json.loads(p.read_text(encoding='utf-8'))
print(f"updated_at: {data.get('updated_at')}")
print(f"mode: {data.get('mode')} | dry_run: {data.get('dry_run')}")
print()
for obs in data.get('observations', []):
    print(f"[{obs.get('symbol')}] {obs.get('status')} / watch={obs.get('watch_side')} / trend={obs.get('trend_mode')}")
    print(f"  {obs.get('human')}")
    print(f"  now={obs.get('current_price')} L={obs.get('long_target')} S={obs.get('short_target')} watch_distance={obs.get('watch_distance')} ({obs.get('watch_distance_pct')}%) score={obs.get('survival_signal_score')}")
    blockers = obs.get('blockers') or []
    if blockers:
        print(f"  blockers: {', '.join(blockers)}")
    print()
PY
else
  echo "no data/market_observer.json yet"
fi

echo "===== signal distance csv tail ====="
if [ -f logs/signal_distance.csv ]; then
  tail -n 10 logs/signal_distance.csv
else
  echo "no logs/signal_distance.csv yet"
fi
