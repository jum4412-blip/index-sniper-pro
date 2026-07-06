#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
cp .env ".env.bak.disable_opposite_close.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
sed -i '/^OPPOSITE_SIGNAL_EXIT_ENABLED=/d' .env 2>/dev/null || true
sed -i '/^OPPOSITE_SIGNAL_EXIT_MODE=/d' .env 2>/dev/null || true
sed -i '/^OPPOSITE_SIGNAL_EXIT_COOLDOWN_UNTIL_NEXT_DAY=/d' .env 2>/dev/null || true
cat >> .env <<'EOF'

# ===== v2.9 OPPOSITE SIGNAL CLOSE-ONLY DISABLED =====
OPPOSITE_SIGNAL_EXIT_ENABLED=false
EOF

echo "opposite-signal close-only disabled"
grep -E 'OPPOSITE_SIGNAL_EXIT' .env || true
