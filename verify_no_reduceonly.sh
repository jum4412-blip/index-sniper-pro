#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
find index_sniper -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
if grep -R "reduceOnly" -n index_sniper main.py; then
  echo "🛑 reduceOnly string remains in executable code"
  exit 1
else
  echo "✅ executable code has no reduceOnly string"
fi
