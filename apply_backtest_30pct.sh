#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo "❌ .env not found. Copy .env.example to .env first."
  exit 1
fi
cp .env ".env.bak.$(date +%Y%m%d_%H%M%S)"
# Remove old/duplicate BT settings and append clean backtest-only settings.
sed -i '/^BT_INITIAL_EQUITY=/d' .env
sed -i '/^BT_CAPITAL_RATIO=/d' .env
sed -i '/^BT_LEVERAGE=/d' .env
sed -i '/^BT_MAX_ORDER_NOTIONAL_USDT=/d' .env
sed -i '/^BT_K_VALUE=/d' .env
sed -i '/^BT_LONG_ONLY_SYMBOLS=/d' .env
sed -i '/^BT_SHORT_ONLY_SYMBOLS=/d' .env
sed -i '/^BT_LONG_DISABLED_SYMBOLS=/d' .env
sed -i '/^BT_SHORT_DISABLED_SYMBOLS=/d' .env
cat >> .env <<'EOF'

# === BACKTEST SETTINGS ONLY ===
BT_INITIAL_EQUITY=1374
BT_CAPITAL_RATIO=0.30
BT_LEVERAGE=5
BT_MAX_ORDER_NOTIONAL_USDT=1000
BT_K_VALUE=0.50
EOF

echo "✅ backtest 30% settings applied"
grep -E 'BT_INITIAL_EQUITY|BT_CAPITAL_RATIO|BT_LEVERAGE|BT_MAX_ORDER_NOTIONAL_USDT|BT_K_VALUE' .env
