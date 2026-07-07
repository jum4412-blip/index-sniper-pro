#!/usr/bin/env bash
set -euo pipefail
for s in $(screen -ls | awk '/v51-vwap/ {print $1}'); do
  screen -S "$s" -X quit || true
done
screen -wipe >/dev/null 2>&1 || true
echo "stopped v51-vwap screens"
