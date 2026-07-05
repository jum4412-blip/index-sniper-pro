#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

cp .env ".env.bak.utcday.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true

for k in BTC_UTC_DAY_AGGREGATION_ENABLED UTC_DAY_INTRADAY_INTERVAL UTC_DAY_INTRADAY_LIMIT DAILY_TARGET_ALERT_ENABLED; do
  sed -i "/^${k}=/d" .env 2>/dev/null || true
done

cat >> .env <<'EOF'

# ===== BTC UTC 00:00 / KST 09:00 DAILY CANDLE PATCH =====
BTC_UTC_DAY_AGGREGATION_ENABLED=true
UTC_DAY_INTRADAY_INTERVAL=1H
UTC_DAY_INTRADAY_LIMIT=500
DAILY_TARGET_ALERT_ENABLED=true
EOF

echo "✅ BTC UTC day aggregation enabled"
grep -E 'BTC_UTC_DAY_AGGREGATION_ENABLED|UTC_DAY_INTRADAY_INTERVAL|UTC_DAY_INTRADAY_LIMIT|DAILY_TARGET_ALERT_ENABLED' .env
