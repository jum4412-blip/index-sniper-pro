#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

cp .env ".env.bak.opposite_close.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true

for key in \
  OPPOSITE_SIGNAL_EXIT_ENABLED \
  OPPOSITE_SIGNAL_EXIT_MODE \
  OPPOSITE_SIGNAL_EXIT_COOLDOWN_UNTIL_NEXT_DAY \
  NOTIFY_BLOCKED_SIGNAL
  do
    sed -i "/^${key}=/d" .env 2>/dev/null || true
  done

cat >> .env <<'EOF'

# ===== v2.9 OPPOSITE SIGNAL CLOSE-ONLY =====
# 반대 신호가 확정되면 기존 반대 포지션만 시장가 청산한다.
# 예: LONG 보유 중 SHORT 신호 확정 -> LONG 청산. 바로 SHORT 진입은 하지 않음.
OPPOSITE_SIGNAL_EXIT_ENABLED=true
OPPOSITE_SIGNAL_EXIT_MODE=close_only
OPPOSITE_SIGNAL_EXIT_COOLDOWN_UNTIL_NEXT_DAY=true
NOTIFY_BLOCKED_SIGNAL=true
EOF

echo "v2.9 opposite-signal close-only env applied"
grep -E 'OPPOSITE_SIGNAL_EXIT|NOTIFY_BLOCKED_SIGNAL' .env || true
