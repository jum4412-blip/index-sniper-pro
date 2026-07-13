#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -f "$ROOT/data/v63_dual_live/PAUSE_NEW_ENTRIES"
echo "▶️ v6.3 신규 진입 재개"
