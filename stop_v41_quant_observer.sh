#!/usr/bin/env bash
set -euo pipefail
screen -S quant-v41 -X quit >/dev/null 2>&1 || true
screen -wipe >/dev/null 2>&1 || true
echo "✅ v4.1 quant observer stopped"
