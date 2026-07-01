#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
rm -rf index_sniper/orders
find index_sniper -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
echo "✅ legacy files cleaned"
echo "remaining one-way close flag grep:"
grep -R "reduceOnly" -n index_sniper main.py || true
