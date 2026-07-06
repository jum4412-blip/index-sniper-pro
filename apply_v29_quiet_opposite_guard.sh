#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p local_backups data
if [ -f .env ]; then
  cp .env "local_backups/.env.bak.v29.$(date +%Y%m%d_%H%M%S)"
fi

set_env() {
  local key="$1"
  local value="$2"
  if [ -f .env ]; then
    sed -i "/^${key}=/d" .env
  fi
  printf '%s=%s\n' "$key" "$value" >> .env
}

# Quiet mode: loop start/heartbeat/HOLD/blocked-signal spam off.
set_env NOTIFY_LOOP_START false
set_env NOTIFY_HEARTBEAT false
set_env NOTIFY_HOLD_SUMMARY false
set_env NOTIFY_BLOCKED_SIGNAL false
set_env NOTIFY_SIGNAL true
set_env NOTIFY_ERROR true
set_env STRATEGY_HEARTBEAT_MINUTES 360

# Alert throttle: repeated identical errors/signals are suppressed for the cooldown window.
set_env ALERT_THROTTLE_ENABLED true
set_env ALERT_ERROR_COOLDOWN_MINUTES 30
set_env ALERT_BLOCKED_SIGNAL_COOLDOWN_MINUTES 360
set_env ALERT_ACTIVE_SIGNAL_COOLDOWN_MINUTES 60
set_env ALERT_HOLD_COOLDOWN_MINUTES 360
set_env ALERT_STATE_PATH data/alert_throttle_v29.json

# Safety: never add a new entry when a position already exists on the same symbol.
# If LONG is open and SHORT signal appears, the bot blocks the new SHORT order and alerts according to throttle.
set_env BLOCK_NEW_ENTRY_WHEN_ANY_POSITION_OPEN true
set_env BLOCK_OPPOSITE_SIGNAL_WHEN_POSITION_OPEN true

rm -f data/alert_throttle_v29.json 2>/dev/null || true

chmod +x run_live_preflight.sh start_live_guarded.sh stop_sniper.sh 2>/dev/null || true

echo "✅ v2.9 quiet/opposite-guard settings applied"
echo "확인: grep -E 'NOTIFY_|ALERT_|BLOCK_NEW_ENTRY|BLOCK_OPPOSITE' .env"
