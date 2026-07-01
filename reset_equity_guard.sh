#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
rm -f data/equity_guard.json
mkdir -p data
echo "✅ equity guard reset: data/equity_guard.json removed"
