#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$ROOT/data/v63_dual_live"
printf 'paused_at=%s\n' "$(date -Is)" > "$ROOT/data/v63_dual_live/PAUSE_NEW_ENTRIES"
chmod 600 "$ROOT/data/v63_dual_live/PAUSE_NEW_ENTRIES"
echo "⏸️ v6.3 신규 진입 중지. 기존 포지션 관리는 계속됩니다."
