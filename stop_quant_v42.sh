#!/usr/bin/env bash
set -euo pipefail
for s in $(screen -ls | awk '/quant-v42/ {print $1}'); do screen -S "$s" -X quit || true; done
echo "stopped quant-v42 screens"
screen -ls || true
